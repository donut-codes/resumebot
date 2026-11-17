"""Where to look finds a role's source without re-reading what has not changed (docs/PRD.md 30.3): a link is parsed
once however many requests ask, and a site finds the hosts it covers by a suffix index, not by a scan of every host.
The answer is the same as before: a literal expected list for a made-up world with every kind of link. Every employer,
host and role is made up (example.test); no network."""

from sqlalchemy import select

from app import db
from app.jobsearch import ats
from app.jobsearch.models import Opportunity
from app.sites import avoid, where, where_view
from tests.sites.builders import SYSTEM_A, SYSTEM_B, SYSTEM_C, UNREAD, board_link, posting_link
from tests.sites.test_where_sources import _linked, _role, _world
from tests.support import OWNER


async def _fill(session) -> dict:
    world = await _world(session)
    employer, sites = world["employers"], world["sites"]
    await _role(session, employer["acme"].id, SYSTEM_A, hours=1, title="Posting acme")
    await _role(session, employer["borealis"].id, "own_site", hours=2, title="Posting borealis")
    await _linked(session, posting_link(SYSTEM_A, "acme", 1), "Shared acme", hours=3)
    await _linked(session, posting_link(SYSTEM_A, "acmeco", 2), "Shared acmeco", hours=4)
    await _linked(session, "https://eu.board.example.test/jobs/5", "Subdomain", hours=5)
    await _linked(session, "https://WWW.board.example.test/jobs/6/", "Front", hours=6)
    await _linked(session, "https://notboard.example.test/jobs/7", "Lookalike host", hours=7)
    await _linked(session, "https://bad.example.test/jobs/8", "Avoided", hours=8)
    await _linked(session, "https://golf.example.test/jobs/9", "Employer site", hours=9)
    await _linked(session, "https://golf.example.test/jobs/10", "Gated", hours=10, status="gated_out")
    await _linked(session, "not a link at all", "No host", hours=11)
    await _linked(session, posting_link(SYSTEM_B, "cirrus", 3), "Second system link", hours=12)
    sites["shared"] = await where.add(session, OWNER, board_link(SYSTEM_A, "acme"))
    await session.flush()
    return world


EXPECTED = {
    "acme": [], "borealis": ["Posting borealis"], "cirrus": [], "delta": [], "echo": [], "foxtrot": [],
    "golf": ["Employer site"], "hotel": [], "board": ["Front", "Subdomain"], "bad": ["Avoided"],
    "shared": ["Shared acme"],
}
EXPECTED_SYSTEMS = {SYSTEM_A: ["Shared acmeco", "Shared acme", "Posting acme"],
                    SYSTEM_B: ["Second system link"], UNREAD: [], SYSTEM_C: []}


async def _names(session, found):
    titles = dict((await session.execute(select(Opportunity.id, Opportunity.title))).all())
    return {k: [titles[i] for i in ids] for k, ids in found.items()}


async def test_every_source_has_the_roles_it_had_before_the_links_were_remembered(js):
    async with db.session_factory()() as session:
        world = await _fill(session)
        by_key = {str(s.id): name for name, s in world["sites"].items()}
        found = await _names(session, await where_view.roles_by_source(
            session, OWNER, sites=list(world["sites"].values()), systems=list(EXPECTED_SYSTEMS)))
        assert {by_key[k]: v for k, v in found.items() if k in by_key} == EXPECTED
        assert {k: v for k, v in found.items() if k in EXPECTED_SYSTEMS} == EXPECTED_SYSTEMS
        again = await _names(session, await where_view.roles_by_source(
            session, OWNER, sites=list(world["sites"].values()), systems=list(EXPECTED_SYSTEMS)))
        assert again == found, "the second request, from what was remembered, says the same"


async def test_a_link_is_parsed_once_across_requests(js, monkeypatch):
    where_view._board_of.cache_clear()
    where_view._place_of.cache_clear()
    async with db.session_factory()() as session:
        world = await _fill(session)
        calls = []
        real = ats.parse
        monkeypatch.setattr(ats, "parse", lambda url: calls.append(url) or real(url))
        sites = list(world["sites"].values())
        for _ in range(2):
            await where_view.roles_by_source(session, OWNER, sites=sites, systems=[SYSTEM_A])
        assert calls and len(calls) == len(set(calls)), "each distinct link or address once, over two requests"


def test_the_suffix_index_finds_exactly_the_hosts_avoid_covers():
    hosts = ["a.test", "x.a.test", "ax.test", "b.x.a.test", "a.test.evil", "test", "x..a.test", "eu.board.example.test"]
    index = where_view._suffixes(hosts)
    for name in ["a.test", "x.a.test", "test", "ax.test", "board.example.test", ".a.test", "nope.test", "evil"]:
        assert sorted(index.get(name, [])) == sorted(h for h in hosts if avoid.covers(name, h)), name


async def test_whole_page_sends_no_more_statements_than_before(client, transactions):
    async with db.session_factory()() as session:
        await _fill(session)
        await session.commit()
    transactions.clear()
    assert (await client.get("/api/v1/sites")).status_code == 200
    sent = [s for s in transactions.open + [q for t in transactions.ended for q in t]
            if s.lstrip().upper().startswith("SELECT")]
    assert len(sent) <= 30
