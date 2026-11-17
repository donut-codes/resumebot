"""`where.seed_employer_sites` plans in a reader and takes the writer's turn only to write. Every employer and
address here is made up; no network."""

from sqlalchemy import select

from app import db
from app.jobsearch.employers import core as employers
from app.sites import where
from app.sites.models import Site, SiteSkill
from tests.sites.test_auto_watch import _employer
from tests.support import EXAMPLE_SYSTEM, OWNER


class Turns:
    """The writer's turns taken while the block runs."""

    def __enter__(self):
        self.count = 0
        db.turn_watchers.append(self._seen)
        return self

    def __exit__(self, *exc):
        db.turn_watchers.remove(self._seen)

    def _seen(self, _waited: float) -> None:
        self.count += 1


async def _employers_with_boards(names: list[str]) -> None:
    async with db.session_factory()() as session:
        for name in names:
            employer = await _employer(session, name)
            employer.ats, employer.board = EXAMPLE_SYSTEM, {"slug": name.lower()}
        await session.commit()


async def test_a_seed_with_every_site_and_skill_made_takes_no_turn(js):
    await _employers_with_boards(["Acme", "Globex", "Hooli"])
    async with db.session_factory()() as session:
        assert await where.seed_employer_sites(session, OWNER) == {"sites": 3, "skills": 3}
        await session.commit()
    with Turns() as turns:
        async with db.session_factory()() as session:
            assert await where.seed_employer_sites(session, OWNER) == {"sites": 0, "skills": 0}
            assert not session.in_transaction()
    assert turns.count == 0


async def test_one_employer_without_a_site_takes_one_turn_and_gets_a_site_and_a_skill(js):
    await _employers_with_boards(["Acme", "Globex"])
    async with db.session_factory()() as session:
        await where.seed_employer_sites(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        await _employer(session, "Initech")
        (await employers.find_or_make(session, OWNER, "Initech")).ats = EXAMPLE_SYSTEM
        (await employers.find_or_make(session, OWNER, "Initech")).board = {"slug": "initech"}
        await session.commit()
    with Turns() as turns:
        async with db.session_factory()() as session:
            assert await where.seed_employer_sites(session, OWNER) == {"sites": 1, "skills": 1}
            await session.commit()
    assert turns.count == 1
    async with db.session_factory()() as session:
        sites = list((await session.scalars(select(Site).where(Site.owner == OWNER).order_by(Site.label))).all())
        assert [s.label for s in sites] == ["Acme", "Globex", "Initech"]
        skills = list((await session.scalars(select(SiteSkill).where(SiteSkill.site_id == sites[2].id))).all())
        assert [(s.version, s.recipe) for s in skills] == [
            (1, {"kind": "adapter", "ats": EXAMPLE_SYSTEM, "board": {"slug": "initech"}})]


async def test_an_employer_whose_board_changed_between_the_plan_and_the_write_gets_the_skill_for_its_board_now(
        js, monkeypatch):
    await _employers_with_boards(["Acme"])
    real = where._plan_seed

    async def plan_then_change(reader, owner):
        planned = await real(reader, owner)
        async with db.session_factory()() as other:
            (await employers.find_or_make(other, OWNER, "Acme")).board = {"slug": "acme-new"}
            await other.commit()
        return planned

    monkeypatch.setattr(where, "_plan_seed", plan_then_change)
    async with db.session_factory()() as session:
        assert await where.seed_employer_sites(session, OWNER) == {"sites": 1, "skills": 1}
        await session.commit()
    async with db.session_factory()() as session:
        recipes = [s.recipe for s in (await session.scalars(select(SiteSkill))).all()]
    assert recipes == [{"kind": "adapter", "ats": EXAMPLE_SYSTEM, "board": {"slug": "acme-new"}}]


async def test_an_employer_gated_between_the_plan_and_the_write_gets_no_site(js, monkeypatch):
    await _employers_with_boards(["Acme", "Globex"])
    real = where._plan_seed

    async def plan_then_gate(reader, owner):
        planned = await real(reader, owner)
        async with db.session_factory()() as other:
            (await employers.find_or_make(other, OWNER, "Acme")).gated = True
            await other.commit()
        return planned

    monkeypatch.setattr(where, "_plan_seed", plan_then_gate)
    async with db.session_factory()() as session:
        assert await where.seed_employer_sites(session, OWNER) == {"sites": 1, "skills": 1}
        await session.commit()
    async with db.session_factory()() as session:
        assert [s.label for s in (await session.scalars(select(Site))).all()] == ["Globex"]


async def test_settling_the_watch_after_a_release_plans_in_a_reader_when_nothing_is_to_switch(js):
    await _employers_with_boards(["Acme"])
    async with db.session_factory()() as session:
        await where.seed_employer_sites(session, OWNER)
        await db.release_if_free(session)
        with Turns() as turns:
            await where.settle_watch(session, OWNER)
        assert not session.in_transaction()
    assert turns.count == 0
