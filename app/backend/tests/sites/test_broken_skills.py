"""A skill that breaks reopens its project (docs/PRD.md 29.4): the night's read says a site refused, changed shape or
answered nothing where it had roles, so the version in force is retired; the site waits, the read leaves it alone, and
only the next night's Scout mission starts its new project (three a night at most; never at once). A board that is
gone, or unreachable tonight, retires nothing. Adapters are stand-ins and every site is made up; no network."""

from dataclasses import dataclass, field

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import actions, adapters
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher, Refused
from app.jobsearch.missions import core as missions
from app.jobsearch.missions.kinds import scout
from app.jobsearch.models import Mission
from app.jobsearch.postings import index as postings
from app.sites import autostart, escalation, projects, skills, where, where_view
from app.sites.models import PlanTask, Project, Site, SiteSkill
from tests.support import EXAMPLE_SYSTEM, OWNER, load_profile


@dataclass
class Listing:
    postings: list[dict]
    truncated: bool = False
    note: str | None = None


@dataclass
class Scripted:
    """Each board's answers in turn: a number is that many postings, an exception is raised."""

    answers: dict = field(default_factory=dict)
    asked: list = field(default_factory=list)

    async def read(self, fetch, board):
        slug = board["slug"]
        self.asked.append(slug)
        answer = self.answers[slug].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Listing([{"ext_id": str(n), "title": f"Role {n} at {slug}", "location": "Remote", "work_mode": "remote",
                         "comp": None, "comp_min": None, "comp_max": None, "posted": None,
                         "url": f"https://jobs.example.test/{slug}/{n}", "apply_url": None, "text": "About it.",
                         "department": None} for n in range(answer)])

    async def detail(self, fetch, board, posting):
        return {}


@pytest.fixture
def board(monkeypatch):
    fake = Scripted()
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: fake})
    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0))
    return fake


async def _watched(session, *names):
    """Each name: an employer on a known system whose site is on, with its reader as version 1 of its skill."""
    out = {}
    for name in names:
        employer = await employers.find_or_make(session, OWNER, name)
        employer.careers_url = f"https://{name.lower()}.example.test/jobs"
        employer.ats, employer.board, employer.check_directly = EXAMPLE_SYSTEM, {"slug": name.lower()}, True
        out[name] = employer
    await session.flush()
    await where.seed_employer_sites(session, OWNER)
    return out


async def _site(session, employer):
    return (await session.scalars(select(Site).where(Site.employer_id == employer.id))).one()


async def _state(session, site):
    [row] = await where_view.listing(session, OWNER, site.id)
    return row["state"]


async def _read(session):
    return await actions.read_boards(session, None, {"owner": OWNER})


async def _scout_mission(session) -> Mission:
    await load_profile(session, OWNER)
    await roster.ensure_roster(session, OWNER)
    await missions.ensure(session, OWNER)
    return await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == missions.SCOUT))


def test_what_a_failed_read_says_about_the_skill():
    assert postings.fault_kind(Refused("acme.example refused the request (403)")) == "refused"
    assert postings.fault_kind(adapters.AdapterError("The board changed shape")) == "shape"
    assert postings.fault_kind(adapters.AdapterError("The board answered 403", 403)) == "refused"
    assert postings.fault_kind(adapters.AdapterError("The board answered 404", 404)) == "gone"
    assert postings.fault_kind(adapters.AdapterError("The board answered 503", 503)) == "network"
    assert postings.fault_kind(httpx.ConnectTimeout("slow")) == "network"
    assert postings.fault_kind(ValueError("not json")) == "shape"
    assert set(postings.BREAKS) == {"refused", "shape", "empty"}


async def test_a_read_that_breaks_a_skill_retires_it_and_only_the_next_scout_mission_starts_the_project(js, board):
    board.answers = {"alpha": [2, 2], "bravo": [adapters.AdapterError("The board changed shape"), 2],
                     "charlie": [adapters.AdapterError("The board answered 503", 503), 2],
                     "delta": [adapters.AdapterError("The board answered 404", 404)]}
    async with db.session_factory()() as session:
        world = await _watched(session, "Alpha", "Bravo", "Charlie", "Delta")
        sites = {n: await _site(session, e) for n, e in world.items()}
        read = await _read(session)
        assert read["employers"] == 4 and read["failed"] == 3
        assert {f["employer"]: f["kind"] for f in read["faults"]} == {
            "Bravo": "shape", "Charlie": "network", "Delta": "gone"}
        assert await skills.current_skill(session, sites["Bravo"].id) is None, "a changed shape retires the skill"
        retired = await skills.broken(session, sites["Bravo"].id)
        assert retired.version == 1 and retired.retired_at is not None, "kept, not deleted"
        for name in ("Alpha", "Charlie", "Delta"):
            assert (await skills.current_skill(session, sites[name].id)).version == 1, (
                f"{name}: a site that read, or was unreachable, or whose board is gone, keeps its skill")
        assert world["Delta"].ats is None, "a board that is gone still goes through resolve.forget"
        # nothing started now: the site waits for tonight's mission
        assert (await session.scalars(select(Project))).all() == []
        assert await _state(session, sites["Bravo"]) == "waiting"
        assert await _state(session, sites["Alpha"]) == "working"
        # the next night's read leaves the broken site alone, and reads the rest
        board.asked.clear()
        second = await _read(session)
        assert sorted(board.asked) == ["alpha", "charlie"] and second["employers"] == 2 and second["failed"] == 0
        assert (await session.scalars(select(Project))).all() == [], "the read starts no project"
        # the Scout mission does: one project, for the site that broke, told why
        entry = await missions.start(session, OWNER, await _scout_mission(session), by="you")
        [project] = (await session.scalars(select(Project))).all()
        assert project.site_id == sites["Bravo"].id and entry.started["site_projects"] == [str(project.id)]
        task = (await session.scalars(select(PlanTask).where(PlanTask.project_id == project.id))).first()
        said = task.context["context"]
        assert "version 1" in said and "stopped working" in said and "changed shape" in said
        assert await _state(session, sites["Bravo"]) == "learning"
        assert await scout.start_sites(session, OWNER) == [], "and while it is open, no second one"


async def test_a_read_that_returns_nothing_where_it_had_roles_or_is_refused_retires_the_skill(js, board):
    board.answers = {"echo": [3, 0], "foxtrot": [3, Refused("foxtrot.example refused the request (403)")]}
    async with db.session_factory()() as session:
        world = await _watched(session, "Echo", "Foxtrot")
        await _read(session)
        for employer in world.values():
            assert (await skills.current_skill(session, (await _site(session, employer)).id)).version == 1
        second = await _read(session)
        assert {f["employer"]: f["kind"] for f in second["faults"]} == {"Echo": "empty", "Foxtrot": "refused"}
        for employer in world.values():
            site = await _site(session, employer)
            assert await skills.current_skill(session, site.id) is None and await _state(session, site) == "waiting"
        made = await autostart.ensure_projects(session, OWNER, limit=3, by_switch=True)
        assert sorted(p.title for p in made) == ["Read echo.example.test", "Read foxtrot.example.test"]


async def test_three_sites_a_night_start_their_projects_and_the_rest_wait_for_the_next_night(js, board):
    names = ("Golf", "Hotel", "India", "Juliet", "Kilo")
    board.answers = {n.lower(): [adapters.AdapterError("changed shape")] for n in names}
    async with db.session_factory()() as session:
        world = await _watched(session, *names)
        assert (await _read(session))["failed"] == 5
        for employer in world.values():
            assert await skills.current_skill(session, (await _site(session, employer)).id) is None
        first = await scout.start_sites(session, OWNER)
        assert len(first) == missions.SITE_PROJECTS_A_NIGHT == 3
        second = await scout.start_sites(session, OWNER)
        assert len(second) == 2 and not set(first) & set(second), "the other two the next night"
        assert await scout.start_sites(session, OWNER) == []


async def test_a_project_that_ended_with_no_skill_is_not_reopened_by_what_broke_before_it_but_by_what_breaks_after(js):
    recipe = {"kind": "adapter", "ats": EXAMPLE_SYSTEM, "board": {"slug": "lima"}}
    async with db.session_factory()() as session:
        world = await _watched(session, "Lima")
        site = await _site(session, world["Lima"])
        # the seeded reader breaks, a project is started for it, and the owner stops that project with no skill made
        await skills.retire_skill(session, await skills.current_skill(session, site.id))
        [first] = await autostart.ensure_projects(session, OWNER, by_switch=True)
        await escalation.stop_for_good(session, first)
        assert await _state(session, site) == "stopped", "what broke before the project is its business"
        assert await autostart.ensure_projects(session, OWNER, by_switch=True) == []
        # a skill is made another way, and then it breaks: that starts the next cycle
        fresh = await skills.save_skill(session, OWNER, site.id, recipe, None)
        assert await _state(session, site) == "working"
        retired = await where.retire_broken(session, OWNER, [
            {"employer_id": str(world["Lima"].id), "employer": "Lima", "kind": "empty", "why": "answered none"}])
        assert retired == ["Lima"] and fresh.retired_at is not None
        assert await _state(session, site) == "waiting", "its skill broke after its last project ended"
        [again] = await autostart.ensure_projects(session, OWNER, by_switch=True)
        assert again.site_id == site.id and again.id != first.id
        assert await _state(session, site) == "learning"
        versions = (await session.scalars(select(SiteSkill).where(SiteSkill.site_id == site.id))).all()
        assert sorted((v.version, v.retired_at is not None) for v in versions) == [(1, True), (2, True)]


async def test_only_a_skill_in_force_on_a_switched_on_site_is_retired_and_other_faults_leave_it_alone(js):
    async with db.session_factory()() as session:
        world = await _watched(session, "Mike", "November")
        november = await _site(session, world["November"])
        await where.switch(session, OWNER, november.id, False)
        faults = [{"employer_id": str(e.id), "employer": e.name, "kind": "shape"} for e in world.values()]
        assert await where.retire_broken(session, OWNER, [
            {**faults[0], "kind": "network"}, {**faults[0], "kind": "gone"}, {"kind": "shape"}]) == []
        assert await where.retire_broken(session, OWNER, faults) == ["Mike"], "a switched-off site is not read"
        assert await where.retire_broken(session, OWNER, faults) == [], "nothing left to retire"
        assert await skills.current_skill(session, november.id) is not None
        project = await projects.create_project(
            session, OWNER, (await _site(session, world["Mike"])).id, "Read it", "Read it", created_by="you")
        assert project.id and await autostart.ensure_projects(session, OWNER, by_switch=True) == []
