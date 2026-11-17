"""The employers list filters, pages and counts in the database and reads sites for the page alone: the same rows,
order, totals, counts and industries as the old way (kept here as the reference), in few statements. Made up."""
import pytest
from sqlalchemy import event, func, select

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.models import Employer, Opportunity
from tests.employer_builders import make_employer, make_role, make_site
from tests.support import OWNER

# --- the old way, as the reference --------------------------------------------------------------------------------


def _old_in_view(listed: dict, view: str) -> bool:
    if view == "open":
        return bool(listed["roles_open"])
    if view == "watched":
        return bool(listed["site"] and listed["site"]["on"])
    if view == "hard_pass":
        return listed["gated"]
    if view == "local":
        return listed["local"] is True
    return True


def _old_named(listed: dict, words: str) -> bool:
    text = words.strip().lower()
    return not text or any(text in (n or "").lower() for n in (listed["name"], *listed["aliases"]))


async def _old_rows(session) -> list[dict]:
    by = (Opportunity.owner == OWNER,)
    total = select(Opportunity.employer_id, func.count()).where(*by).group_by(Opportunity.employer_id)
    counts = dict((await session.execute(total)).all())
    open_counts = dict((await session.execute(total.where(Opportunity.status.in_(stages.OPEN)))).all())
    ahead = dict((await session.execute(total.where(Opportunity.status.in_(employers.AHEAD)))).all())
    found = (await session.scalars(select(Employer).where(Employer.owner == OWNER)
                                   .order_by(Employer.name, Employer.id))).all()
    sites = await employers._sites_by_employer(session, OWNER)
    return [{**employers.row(e), "roles_total": counts.get(e.id, 0), "roles_open": open_counts.get(e.id, 0),
             "roles_ahead": ahead.get(e.id, 0), "fetched_at": e.fetched_at.isoformat() if e.fetched_at else None,
             "site": sites.get(e.id)} for e in found]


async def _old_listing(session, *, view="all", q="", industry="", cursor=None, limit=50) -> dict:
    start = int(cursor) if cursor else 0
    everyone = await _old_rows(session)
    shown = [r for r in everyone if _old_in_view(r, view) and _old_named(r, q)
             and (not industry or r["industry"] == industry)]
    end = start + limit
    return {"employers": shown[start:end], "next": str(end) if end < len(shown) else None, "total": len(shown),
            "counts": {v: sum(_old_in_view(r, v) for r in everyone) for v in employers.VIEWS},
            "industries": sorted({r["industry"] for r in everyone if r["industry"]})}


# --- the world --------------------------------------------------------------------------------------------------------


async def _world(session, waiting: bool = True, extra: int = 0):
    """Employers with aliases, a hard pass, a local one, a watched site, an off site and (when asked) a waiting one."""
    acme = await make_employer(session, "Acme", industry="Robotics", aliases=["Acme Robotics", "Zed Works"])
    globex = await make_employer(session, "globex", industry="Banking", local=True)
    initech = await make_employer(session, "Initech", industry="Robotics", gated=True, gated_reason="no")
    umbrella = await make_employer(session, "Umbrella", industry="")
    hooli = await make_employer(session, "Hooli", local=False, aliases=[None, "ACME Parts"])
    session.add_all([make_role(acme, "A1", stages.READY_TO_LAUNCH), make_role(acme, "A2"),
                     make_role(acme, "A3", stages.APPLIED), make_role(globex, "G1", stages.REJECTED),
                     make_role(hooli, "H1", stages.ACTIVE_DISCUSSION),
                     make_site(acme, "https://careers.acme.test/a", on=True),
                     make_site(acme, "https://careers.acme.test/b", on=False),
                     make_site(globex, "https://careers.globex.test", on=False)])
    if waiting:
        session.add(make_site(hooli, "https://careers.hooli.test", on=True))
    for i in range(extra):
        employer = await make_employer(session, f"Filler {i:03}", industry="Robotics" if i % 2 else None)
        session.add(make_role(employer, f"F{i}"))
    await session.commit()
    return [acme, globex, initech, umbrella, hooli]


QUERIES = [{}, *({"view": v} for v in employers.VIEWS), {"q": "acme"}, {"q": " ACME "}, {"q": "zed"},
           {"q": "robotics"}, {"q": "nothing"}, {"view": "watched", "q": "ac"}, {"view": "open", "q": "o"},
           {"industry": "Robotics"}, {"industry": "Robotics", "view": "hard_pass"}, {"industry": "None"},
           {"limit": 2}, {"limit": 2, "cursor": "2"}, {"limit": 2, "cursor": "4"}, {"limit": 2, "cursor": "99"},
           {"limit": 1, "q": "o", "cursor": "1"}]


@pytest.mark.parametrize("waiting", [True, False])
async def test_every_view_name_industry_and_page_matches_the_old_listing(js, waiting):
    async with db.session_factory()() as session:
        await _world(session, waiting=waiting, extra=3)
        for query in QUERIES:
            assert await employers.listing(session, OWNER, **query) == await _old_listing(session, **query), query


async def test_the_counts_the_rail_shows_are_the_old_ones(js):
    async with db.session_factory()() as session:
        await _world(session)
        got = await employers.view_counts(session, OWNER)
        old = (await _old_listing(session))["counts"]
    assert got == old
    assert got["all"] == 5 and got["hard_pass"] == 1 and got["local"] == 1


async def test_rows_for_some_ids_equal_the_old_rows_for_them(js):
    async with db.session_factory()() as session:
        world = await _world(session)
        ids = [world[0].id, world[4].id, world[3].id]
        got = await employers.rows(session, OWNER, ids)
        old = [r for r in await _old_rows(session) if r["id"] in {str(i) for i in ids}]
    assert got == old


async def test_the_list_says_nothing_about_another_owners_employers(js):
    async with db.session_factory()() as session:
        await _world(session)
        other = Employer(owner="somebody-else", key="acme", name="Acme", aliases=[], offices=[], sectors=[],
                         events=[], sources={}, owner_fields=[], gated=True, local=True, industry="Zzz")
        session.add(other)
        await session.commit()
        page = await employers.listing(session, OWNER)
    assert page["total"] == 5 and page["counts"]["hard_pass"] == 1 and "Zzz" not in page["industries"]


# --- the work is bounded ----------------------------------------------------------------------------------------------


async def _statements(session, call) -> list[str]:
    seen: list[str] = []
    engine = session.get_bind()

    def spy(conn, cursor, statement, *rest):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        await call()
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    return seen


async def test_statements_do_not_grow_with_the_employers_and_no_role_row_is_loaded(js):
    async with db.session_factory()() as session:
        await _world(session, waiting=False)
        small = await _statements(session, lambda: employers.listing(session, OWNER, limit=3))
        for i in range(40):
            await make_employer(session, f"Later {i:02}", industry="Banking")
        await session.commit()
        big = await _statements(session, lambda: employers.listing(session, OWNER, limit=3))
        page = await employers.listing(session, OWNER, limit=3)
    assert len(big) == len(small)
    assert len(page["employers"]) == 3 and page["total"] == 45
    roles = [s for s in big if "FROM js_opportunities" in s and "js_opportunities.title" in s]
    assert roles == [], "no role row is loaded for the list, only grouped counts"


async def test_a_name_search_reads_one_more_statement_at_most(js):
    async with db.session_factory()() as session:
        await _world(session, waiting=False)
        plain = await _statements(session, lambda: employers.listing(session, OWNER, limit=3))
        named = await _statements(session, lambda: employers.listing(session, OWNER, q="o", limit=3))
    assert len(named) <= len(plain) + 1


async def test_sites_are_read_for_the_pages_employers_alone(js):
    async with db.session_factory()() as session:
        world = await _world(session, waiting=False)
        for i in range(10):
            session.add(make_site(world[3], f"https://more{i}.umbrella.test", on=False))
        await session.commit()
        page = await employers.listing(session, OWNER, limit=1)
        assert [e["name"] for e in page["employers"]] == ["Acme"]
        seen = await _statements(session, lambda: employers.listing(session, OWNER, limit=1))
    assert any("FROM js_sites" in s and "js_sites.employer_id IN" in s for s in seen)
    built = [s for s in seen if "FROM js_employers" in s and "js_employers.id IN" in s]
    assert built, "the employers a site belongs to are read by id, not all of them"


async def test_a_waiting_site_keeps_the_words_the_whole_queue_gives_it(js):
    async with db.session_factory()() as session:
        world = await _world(session, waiting=True)
        hooli = (await employers.rows(session, OWNER, [world[4].id]))[0]
        whole = (await employers._sites_by_employer(session, OWNER))[world[4].id]
    assert hooli["site"] == whole
    assert hooli["site"]["state"] == "waiting"


async def test_an_unknown_view_is_refused(js):
    async with db.session_factory()() as session:
        with pytest.raises(employers.EmployerError):
            await employers.listing(session, OWNER, view="bogus")
