"""`where.unreadable_employer_ids` asks in a fixed number of statements however many sites the owner has, and gives the
set the per-site loop gave; `not_checked.settle` reads the held boards apart from the writer's turn. Every site and
employer is made up."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import event

from app import db
from app.jobsearch import off_turn
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import not_checked
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import JobPosting
from app.sites import skills, where
from app.sites.models import Site
from tests.support import EXAMPLE_SYSTEM, OWNER, load_profile

SEEN = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)
RECIPE = {"kind": "adapter", "ats": EXAMPLE_SYSTEM, "board": {"slug": "lima"}}


async def _site(session, name: str, *, on: bool = True, employer: bool = True) -> tuple[Site, uuid.UUID | None]:
    employer_id = (await employers.find_or_make(session, OWNER, name)).id if employer else None
    site = Site(owner=OWNER, url=f"https://{name.lower()}.example.test/careers", kind="employer", on=on,
                employer_id=employer_id, label=name, created_at=SEEN, turned_on_at=SEEN if on else None)
    session.add(site)
    await session.flush()
    return site, employer_id


async def _old_way(session, owner: str) -> set[uuid.UUID]:
    """What the function did before: one question for each site."""
    from sqlalchemy import select

    out = set()
    for site in (await session.scalars(
        select(Site).where(Site.owner == owner, Site.on.is_(True), Site.employer_id.is_not(None))
    )).all():
        if await skills.broken(session, site.id) is not None:
            out.add(site.employer_id)
    return out


async def test_the_unreadable_employers_are_the_same_set_in_a_fixed_number_of_statements(js):
    async with db.session_factory()() as session:
        working, working_e = await _site(session, "Working")
        broke, broke_e = await _site(session, "Broke")
        recovered, recovered_e = await _site(session, "Recovered")
        never, _ = await _site(session, "Never")
        off, _ = await _site(session, "Off", on=False)
        _board, _ = await _site(session, "Board", employer=False)
        for site in (working, broke, recovered, off, _board):
            await skills.save_skill(session, OWNER, site.id, RECIPE)
        await skills.retire_skill(session, (await skills.skills_of_site(session, broke.id))[0])
        await skills.retire_skill(session, (await skills.skills_of_site(session, off.id))[0])
        await skills.retire_skill(session, (await skills.skills_of_site(session, _board.id))[0])
        old = await skills.save_skill(session, OWNER, recovered.id, RECIPE)  # version 2 in force beside a retired 1
        await skills.retire_skill(session, (await skills.skills_of_site(session, recovered.id))[0])
        assert old.retired_at is None
        off.on = False  # saving a skill switched it on: the owner has it off
        await session.flush()
        assert never.id is not None and working_e != broke_e

        expected = await _old_way(session, OWNER)
        assert expected == {broke_e}, "only a site with a retired skill and none in force"

        seen: list[str] = []
        engine = db.engine().sync_engine

        def noted(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", noted)
        try:
            assert await where.unreadable_employer_ids(session, OWNER) == expected
            few = len(seen)
            for n in range(6):  # more sites, more of them broken: the count of statements stays
                site, _ = await _site(session, f"More{n}")
                await skills.save_skill(session, OWNER, site.id, RECIPE)
                await skills.retire_skill(session, (await skills.skills_of_site(session, site.id))[0])
            seen.clear()
            assert len(await where.unreadable_employer_ids(session, OWNER)) == 7
            assert len(seen) == few <= 3
        finally:
            event.remove(engine, "before_cursor_execute", noted)


async def test_no_site_means_no_unreadable_employer(js):
    async with db.session_factory()() as session:
        assert await where.unreadable_employer_ids(session, OWNER) == set()


async def test_settle_reads_the_held_boards_apart_and_marks_the_same_postings(js, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        read_site, read_e = await _site(session, "Read")
        gone = (await employers.find_or_make(session, OWNER, "Gone")).id  # no site: nothing reads it
        session.add_all([
            JobPosting(owner=OWNER, employer_id=source, ext_id=name, company=name, title=name, ats=EXAMPLE_SYSTEM,
                       match_key=match_key(name, name), first_seen=SEEN, last_seen=SEEN, links=[],
                       url=f"https://jobs.example.test/{name}")
            for source, name in ((read_e, "Read-1"), (gone, "Gone-1"), (gone, "Gone-2"))])
        await session.commit()
        assert read_site.on

    real = off_turn.reading
    apart: list[bool] = []

    def watching(session, **more):
        manager = real(session, **more)

        class Spy:
            async def __aenter__(self):
                reader = await manager.__aenter__()
                apart.append(reader is not session)
                return reader

            async def __aexit__(self, *exc):
                return await manager.__aexit__(*exc)

        return Spy()

    monkeypatch.setattr(off_turn, "reading", watching)
    async with db.session_factory()() as session:
        assert await not_checked.settle(session, OWNER) == 2
        await session.commit()
        assert apart == [True], "the held set was read in a reader's session, not the writer's"
    async with db.session_factory()() as session:
        from sqlalchemy import select

        marked = {p.ext_id for p in (await session.scalars(select(JobPosting))).all() if p.not_checked_at is not None}
        assert marked == {"Gone-1", "Gone-2"}
