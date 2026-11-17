"""Where to look (docs/PRD.md path 29, step 8): a site for every employer with an address (the ones on a known job
system already working), the owner's switch on each (off unless the owner watched the employer or added the site), the
night's read and its resolution steps taking only the switched-on sites, a project for a site started only in the
night's Scout mission (never by a switch, at most three a night), a project held by a failure not offered as work, and
the states the owner sees. Adapters are stand-ins and every site is made up; no network."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import actions, adapters
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.loops.schedule import schedule_pass, send_knocks
from app.jobsearch.missions import core as missions
from app.jobsearch.missions.kinds import scout
from app.jobsearch.models import JobPosting, Mission, Opportunity
from app.jobsearch.work import session_kinds
from app.models.work import Task
from app.settings import settings
from app.sites import autostart, completion, escalation, failures, projects, round_process, skills, where, where_view
from app.sites import tools as site_tools
from app.sites.models import Project, Site, SiteSkill
from app.timeutil import iso_utc
from tests.sites.builders import SYSTEM_A, SYSTEM_B, UNREAD, board_link, board_of
from tests.support import OWNER, load_profile, pin_clock

CIRRUS_BOARD = board_link(SYSTEM_B, "cirrus")
MAPLE_BOARD = board_link(SYSTEM_A, "maple")


async def _employer(session, name, *, careers=None, board_url=None, system=None, board=None, watched=False,
                    gated=False):
    employer = await employers.find_or_make(session, OWNER, name)
    employer.careers_url, employer.board_url = careers, board_url
    employer.ats, employer.board = system, board or {}
    employer.check_directly, employer.gated = watched, gated
    await session.flush()
    return employer


async def _sites(session) -> dict[str, Site]:
    return {s.url: s for s in (await session.scalars(select(Site).where(Site.owner == OWNER))).all()}


async def _state(session, site) -> str:
    [row] = await where_view.listing(session, OWNER, site.id)
    return row["state"]


async def _world(session):
    """Five employers: a known system and a plain careers page, a watched one, a board only, a system with no reader,
    one with no address and a gated one."""
    return {
        "acme": await _employer(session, "Acme", careers="https://careers.acme.example.test/",
                                system=SYSTEM_A, board={**board_of("acme"), "gone": []}),
        "borealis": await _employer(session, "Borealis", careers="https://borealis.example.test/jobs", watched=True),
        "cirrus": await _employer(session, "Cirrus", board_url=board_link(SYSTEM_B, "cirrus"),
                                  system=SYSTEM_B, board=board_of("cirrus")),
        "delta": await _employer(session, "Delta", careers="https://delta.example.test/careers", system=UNREAD,
                                 board=board_of("delta")),
        "echo": await _employer(session, "Echo"),
        "foxtrot": await _employer(session, "Foxtrot", careers="https://foxtrot.example.test/", gated=True),
    }


# --- a site for every employer --------------------------------------------------------------------------------------


async def test_seeding_makes_one_site_per_employer_with_an_address_and_the_known_systems_work(js):
    async with db.session_factory()() as session:
        world = await _world(session)
        assert await where.seed_employer_sites(session, OWNER) == {"sites": 4, "skills": 2}
        sites = await _sites(session)
        assert set(sites) == {"https://careers.acme.example.test/", "https://borealis.example.test/jobs",
                              CIRRUS_BOARD, "https://delta.example.test/careers"}, (
            "the careers page, else the board; none for no address or a gated employer")
        acme = sites["https://careers.acme.example.test/"]
        assert (acme.kind, acme.employer_id, acme.label, acme.added_by) == ("employer", world["acme"].id, "Acme", "app")
        skill = await skills.current_skill(session, acme.id)
        assert (skill.version, skill.made_by_project_id) == (1, None)
        assert skill.recipe == {"kind": "adapter", "ats": SYSTEM_A, "board": board_of("acme")}, (
            "the board's own bookkeeping is not part of the address")
        assert await skills.current_skill(session, sites["https://delta.example.test/careers"].id) is None, (
            "a system the app has no reader for is learned by a project")
        assert await skills.current_skill(session, sites["https://borealis.example.test/jobs"].id) is None


async def test_a_site_starts_on_for_a_watched_employer_or_a_known_system_and_off_with_no_runbook(js):
    # Edited with PRD 29.10 (a site that holds a runbook is switched on). Old: a site on a known job system started off,
    # and was working only once the owner switched it on. New: its reader switches it on when it is made.
    async with db.session_factory()() as session:
        await _world(session)
        await where.seed_employer_sites(session, OWNER)
        sites = await _sites(session)
        assert {u: s.on for u, s in sites.items()} == {
            "https://careers.acme.example.test/": True, "https://borealis.example.test/jobs": True,
            CIRRUS_BOARD: True, "https://delta.example.test/careers": False}, (
            "Borealis was watched; Acme and Cirrus have the reader of their job system; Delta's has none")
        assert sites["https://borealis.example.test/jobs"].turned_on_at is not None
        assert sites["https://careers.acme.example.test/"].turned_on_at is not None
        assert sites["https://delta.example.test/careers"].turned_on_at is None
        acme = sites["https://careers.acme.example.test/"]
        [row] = await where_view.listing(session, OWNER, acme.id)
        assert (row["state"], row["skill"], row["needs"]) == ("working", {"version": 1, "kind": "adapter"}, None)
        assert await _state(session, sites["https://borealis.example.test/jobs"]) == "waiting"
        assert await _state(session, sites["https://delta.example.test/careers"]) == "off"


async def test_seeding_again_changes_nothing_the_owner_set_and_an_employer_that_gains_a_board_gets_its_reader(js):
    async with db.session_factory()() as session:
        world = await _world(session)
        await where.seed_employer_sites(session, OWNER)
        sites = await _sites(session)
        borealis = sites["https://borealis.example.test/jobs"]
        await where.switch(session, OWNER, borealis.id, False)
        before = {u: (s.id, s.on, s.turned_on_at) for u, s in sites.items()}
        assert await where.seed_employer_sites(session, OWNER) == {"sites": 0, "skills": 0}
        assert {u: (s.id, s.on, s.turned_on_at) for u, s in (await _sites(session)).items()} == before
        assert (await session.scalar(select(SiteSkill.id).where(SiteSkill.site_id == borealis.id))) is None
        # its job system is found later: the same site, now with its reader as version 1
        world["borealis"].ats, world["borealis"].board = SYSTEM_A, board_of("borealis")
        assert await where.seed_employer_sites(session, OWNER) == {"sites": 0, "skills": 1}
        assert (await skills.current_skill(session, borealis.id)).version == 1
        assert await where.seed_employer_sites(session, OWNER) == {"sites": 0, "skills": 0}
        # a version retired because it broke is history, not a reason to add the reader again
        await skills.retire_skill(session, await skills.current_skill(session, borealis.id))
        assert await where.seed_employer_sites(session, OWNER) == {"sites": 0, "skills": 0}
        assert len(await _sites(session)) == 4


async def test_an_address_the_owner_added_becomes_its_employers_site_and_two_employers_do_not_share_one(js):
    async with db.session_factory()() as session:
        added = await where.add(session, OWNER, "https://careers.acme.example.test/", "employer")
        acme = await _employer(session, "Acme", careers="https://careers.acme.example.test/")
        twin = await _employer(session, "Acme Twin", careers="https://careers.acme.example.test/")
        await where.seed_employer_sites(session, OWNER)
        assert len(await _sites(session)) == 1
        assert added.employer_id in (acme.id, twin.id) and added.on and added.added_by == "owner"


# --- adding, switching ----------------------------------------------------------------------------------------------


async def test_adding_a_site_switches_it_on_and_only_a_public_address_is_taken(js):
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, "  https://www.jobs.example.test/remote  ", "job_board")
        assert (site.url, site.kind, site.on, site.added_by) == ("https://www.jobs.example.test/remote", "job_board",
                                                                 True, "owner")
        assert site.turned_on_at is not None and site.employer_id is None
        [row] = await where_view.listing(session, OWNER, site.id)
        assert (row["label"], row["state"], row["roles"], row["employer_id"]) == (
            "jobs.example.test", "waiting", 0, None)
        for bad in ("http://localhost/jobs", "ftp://jobs.example.test/", "https://user:pw@jobs.example.test/",
                    "http://10.0.0.5/jobs", "jobs", "", "https://jobs.example.test/?key=sk-ant-api03-" + "a" * 40):
            with pytest.raises(where.WhereError):
                await where.add(session, OWNER, bad, "job_board")
        for bad in ("https://jobs.example.test/?token=" + "eyJhbGciOiJIUzI1NiJ9." + "a" * 30 + "." + "b" * 30,
                    "https://jobs.example.test/?key=sk-ant-api03-" + "a" * 40):
            with pytest.raises(where.WhereError, match="password or key"):
                await where.add(session, OWNER, bad, "job_board")
        with pytest.raises(where.WhereError, match="job_board"):
            await where.add(session, OWNER, "https://x.example.test/", "blog")
        # adding what is already there switches it on, and adds nothing
        await where.switch(session, OWNER, site.id, False)
        again = await where.add(session, OWNER, "https://www.jobs.example.test/remote", "association")
        assert again.id == site.id and again.on and again.kind == "job_board"
        assert len(await _sites(session)) == 1


async def test_a_careers_address_with_camel_case_words_is_taken(js):
    address = "https://fa-example-prod1.jobs.example.test/hcmUI/CandidateExperience/en/sites/CX_1/jobs"
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, address, "job_board")
        assert site.url == address and site.on


async def test_the_switch_records_when_a_site_was_first_turned_on_and_starts_no_project(js):
    async with db.session_factory()() as session:
        await _world(session)
        await where.seed_employer_sites(session, OWNER)
        # Delta's job system has no reader, so its site starts off (PRD 29.10; Cirrus, which this test used before a
        # reader switched a site on, is on from the start)
        delta = (await _sites(session))["https://delta.example.test/careers"]
        assert delta.turned_on_at is None
        await where.switch(session, OWNER, delta.id, True)
        first = delta.turned_on_at
        assert first is not None and delta.on
        await where.switch(session, OWNER, delta.id, False)
        await where.switch(session, OWNER, delta.id, True)
        assert delta.turned_on_at == first, "the first time stays the first time"
        with pytest.raises(where.NoSuchSite):
            await where.switch(session, OWNER, uuid.uuid4(), True)
        with pytest.raises(where.NoSuchSite):
            await where.switch(session, "someone else", delta.id, False)
        assert (await session.scalars(select(Project))).all() == [], "a switch never starts a project"


async def test_switching_a_site_off_pauses_its_open_project_and_switching_it_on_resumes_it(js):
    """A switched-off site's project kept being handed out. A paused project could not be resumed."""
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, "https://board.example.test/jobs", "job_board")
        conn = await roster.ensure_roster(session, OWNER)
        project = await projects.create_project(session, OWNER, site.id, "Read it", "Read it", created_by="you")
        await where.switch(session, OWNER, site.id, False)
        assert (project.stopped_at is not None, project.stop_reason) == (True, where.SWITCHED_OFF_PAUSE)
        assert await completion.next_task_or_reason(session, project, conn.id) == (None, "stopped")
        await where.switch(session, OWNER, site.id, False)  # again: nothing more to pause
        await where.switch(session, OWNER, site.id, True)
        assert project.stopped_at is None and project.stop_reason is None
        task, _why = await completion.next_task_or_reason(session, project, conn.id)
        assert task is not None and task.title == "Write plan v1"


async def test_switching_a_site_on_resumes_a_project_the_owner_paused_but_not_one_for_a_site_read_in_the_browser(js):
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, "https://board.example.test/jobs", "job_board")
        project = await projects.create_project(session, OWNER, site.id, "Read it", "Read it", created_by="you")
        await completion.stop_project(session, project, "later")
        await where.switch(session, OWNER, site.id, False)
        assert project.stop_reason == "later", "already paused: the switch leaves the owner's pause as it was"
        await where.switch(session, OWNER, site.id, True)
        assert project.stopped_at is None, "switching on carries the paused project on"
        await where.set_read_by(session, OWNER, site.id, "browser")
        assert project.stop_reason == where.BROWSER_PAUSE
        await where.switch(session, OWNER, site.id, False)
        await where.switch(session, OWNER, site.id, True)
        assert project.stop_reason == where.BROWSER_PAUSE, "a browser-read site's project stays paused"


async def test_the_routes_list_add_and_switch_and_a_switch_starts_nothing(client):
    async with db.session_factory()() as session:
        await _world(session)
        await where.seed_employer_sites(session, OWNER)
        await session.commit()
    rows = (await client.get("/api/v1/sites")).json()["sites"]
    assert [r["label"] for r in rows] == ["Borealis"], (
        "the page lists the watched employer only: the other employers' sites are switched on from Employers")
    assert rows[0]["on"]
    assert set(rows[0]) == {
        "id", "url", "kind", "label", "on", "employer_id", "state", "off_by", "off_note", "off_at", "by_code", "status",
        "skill", "needs", "roles", "added_by", "list", "read_by", "read_found", "read_note", "created_at"}
    assert (rows[0]["list"], rows[0]["read_by"]) == ("search", "app")
    made = await client.post("/api/v1/sites", json={"url": "https://board.example.test/jobs", "kind": "job_board"})
    assert made.status_code == 200 and made.json()["on"] is True and made.json()["state"] == "waiting"
    refused = await client.post("/api/v1/sites", json={"url": "http://localhost/jobs", "kind": "job_board"})
    assert refused.status_code == 422 and "public" in refused.json()["detail"]
    async with db.session_factory()() as session:
        acme_id = (await _sites(session))["https://careers.acme.example.test/"].id
    acme = {"id": str(acme_id)}
    on = await client.patch(f"/api/v1/sites/{acme['id']}", json={"on": True})
    assert on.status_code == 200 and (on.json()["on"], on.json()["state"]) == (True, "working")
    off = await client.patch(f"/api/v1/sites/{acme['id']}", json={"on": False})
    assert off.json()["state"] == "off"
    missing = await client.patch("/api/v1/sites/00000000-0000-0000-0000-000000000000", json={"on": True})
    assert missing.status_code == 404
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Project))).all() == []


# --- the night's read: only the switched-on sites -------------------------------------------------------------------


@dataclass
class Listing:
    postings: list[dict]
    truncated: bool = False
    note: str | None = None


@dataclass
class Board:
    asked: list = field(default_factory=list)

    async def read(self, fetch, board):
        self.asked.append(board["slug"])
        return Listing([{"ext_id": "1", "title": f"Role at {board['slug']}", "location": "Remote",
                         "work_mode": "remote", "comp": None, "comp_min": None, "comp_max": None, "posted": None,
                         "url": f"https://jobs.example.test/{board['slug']}/1", "apply_url": None, "text": "About it.",
                         "department": None}])

    async def detail(self, fetch, board, posting):
        return {}


def _mock_fetch(requested: list):
    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=f'<a href="{board_link(SYSTEM_A, "found-it")}">Jobs</a>')

    return lambda: Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)


async def test_the_read_reads_only_employers_whose_site_is_on(js, monkeypatch):
    board = Board()
    monkeypatch.setattr(adapters, "ADAPTERS", {SYSTEM_A: board})
    monkeypatch.setattr(polite, "make", _mock_fetch([]))
    async with db.session_factory()() as session:
        for name in ("Acme", "Borealis", "Cirrus"):
            await _employer(session, name, careers=f"https://{name.lower()}.example.test/jobs", system=SYSTEM_A,
                            board={"slug": name.lower()}, watched=name == "Borealis")
        await where.seed_employer_sites(session, OWNER)
        # Old: Acme's site started off (known system, no switch) and the owner switched Cirrus on. New (PRD 29.10): the
        # reader switches all three on, so the owner switches Acme off, which is what keeps it from being read.
        await where.switch(session, OWNER, (await _sites(session))["https://acme.example.test/jobs"].id, False)
        read = await actions.read_boards(session, None, {"owner": OWNER})
        assert read["employers"] == 2 and sorted(board.asked) == ["borealis", "cirrus"], (
            "Acme is known and has a reader, but its site is off")
        assert {p.company for p in (await session.scalars(select(JobPosting))).all()} == {"Borealis", "Cirrus"}
        # nothing is switched on: the read has nothing to read
        for site in (await _sites(session)).values():
            await where.switch(session, OWNER, site.id, False)
        board.asked.clear()
        assert (await actions.read_boards(session, None, {"owner": OWNER}))["employers"] == 0 and board.asked == []


async def test_the_resolution_steps_look_only_at_switched_on_sites_and_seed_the_site_of_a_found_board(js, monkeypatch):
    requested: list[str] = []
    monkeypatch.setattr(polite, "make", _mock_fetch(requested))
    async with db.session_factory()() as session:
        on = await _employer(session, "Pine", careers="https://pine.example.test/jobs", watched=True)
        off = await _employer(session, "Oak", careers="https://oak.example.test/jobs")
        linked = await _employer(session, "Maple", careers=MAPLE_BOARD)
        await session.flush()
        found = await actions.resolve_links(session, None, {"owner": OWNER})
        assert requested.count("pine.example.test") >= 1 and "oak.example.test" not in requested, (
            "an off site's careers page is not read")
        assert (on.ats, off.ats) == (SYSTEM_A, None) and found["found"] == 2
        # Maple's system came from a link it already held, with no network: it has its site now, with its reader
        assert (linked.ats, linked.board) == (SYSTEM_A, board_of("maple"))
        sites = await _sites(session)
        # Old: off, whatever its reader. New (PRD 29.10): the reader it was given switches it on.
        assert sites[MAPLE_BOARD].on is True
        assert (await skills.current_skill(session, sites[MAPLE_BOARD].id)).version == 1
        assert (await skills.current_skill(session, sites["https://pine.example.test/jobs"].id)).version == 1
        assert await skills.current_skill(session, sites["https://oak.example.test/jobs"].id) is None


# --- a project starts only in the night's Scout mission -------------------------------------------------------------


async def _mission(session, key) -> Mission:
    return await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == key))


async def _ready(session):
    await load_profile(session, OWNER)
    await roster.ensure_roster(session, OWNER)
    await missions.ensure(session, OWNER)


async def _on_sites(session, count):
    """`count` switched-on sites, each its own employer with a careers page and no job system."""
    out = []
    for n in range(count):
        await _employer(session, f"Employer {chr(65 + n)}", careers=f"https://e{n}.example.test/jobs", watched=True)
    await where.seed_employer_sites(session, OWNER)
    for site in sorted((await _sites(session)).values(), key=lambda s: s.url):
        out.append(site)
    return out


async def test_projects_start_in_the_scout_mission_only_for_first_time_on_sites_and_three_a_night(js, knocks):
    async with db.session_factory()() as session:
        await _ready(session)
        sites = await _on_sites(session, 5)
        off = await where.add(session, OWNER, "https://off.example.test/jobs", "job_board")
        await where.switch(session, OWNER, off.id, False)
        assert [await _state(session, s) for s in sites] == ["waiting"] * 5
        assert (await session.scalars(select(Project))).all() == [], "nothing starts at switch or seed time"
        # no other mission starts one
        for key in (missions.READ, missions.SWEEP):
            await missions.start(session, OWNER, await _mission(session, key), by="you")
        assert (await session.scalars(select(Project))).all() == []
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        made = (await session.scalars(select(Project).order_by(Project.created_at))).all()
        assert len(made) == missions.SITE_PROJECTS_A_NIGHT == 3
        assert {p.site_id for p in made} <= {s.id for s in sites} and all(p.created_by == "you" for p in made)
        assert sorted(entry.started["site_projects"]) == sorted(str(p.id) for p in made)  # one instant: no order
        assert "Started learning 3 sites." in entry.summary
        # the next night's start (the search is still open: it is skipped) gets the rest, and then nothing more
        again = await missions.start(session, OWNER, scout, by="you")
        assert again.outcome == "skipped" and len(again.started["site_projects"]) == 2
        assert "Started learning 2 sites." in again.summary
        third = await missions.start(session, OWNER, scout, by="you")
        assert third.started["site_projects"] == [] and "Started learning" not in third.summary
        assert len((await session.scalars(select(Project))).all()) == 5, "the switched-off site never got one"
        assert {p.site_id for p in await session.scalars(select(Project))} == {s.id for s in sites}


async def test_the_owners_sites_to_learn_a_night_decide_how_many_start_and_minus_one_starts_every_waiting_site(js):
    from app.jobsearch.profile import store as profile_store
    from app.jobsearch.profile.schema import Settings

    assert Settings().site_projects_per_night == missions.SITE_PROJECTS_A_NIGHT == 3
    async with db.session_factory()() as session:
        await _ready(session)
        await _on_sites(session, 7)
        await profile_store.save(session, OWNER, "settings", {"site_projects_per_night": 2}, by="test")
        assert len(await scout.start_sites(session, OWNER)) == 2, "their number, not the default"
        assert len(await scout.start_sites(session, OWNER)) == 2, "and the next night's two"
        await profile_store.save(session, OWNER, "settings", {"site_projects_per_night": -1}, by="test")
        assert len(await scout.start_sites(session, OWNER)) == 3, "no limit: all that are left, at once"
        assert await scout.start_sites(session, OWNER) == []
        assert len((await session.scalars(select(Project))).all()) == 7


async def test_a_night_of_no_limit_on_sites_to_learn_says_starts_tonight_for_every_waiting_site(js):
    from app.jobsearch.profile import store as profile_store

    async with db.session_factory()() as session:
        await _ready(session)
        sites = await _on_sites(session, 8)
        scout = await _mission(session, missions.SCOUT)
        await missions.resume(session, scout, by="Tester")

        async def said() -> set[str]:
            return {row["status"].split(" at ")[0] for row in await where_view.listing(session, OWNER)
                    if row["state"] == "waiting"}

        assert len(sites) == 8 and await said() == {
            "Starts tonight", "Waits its turn: starts in about 1 night", "Waits its turn: starts in about 2 nights"}
        await profile_store.save(session, OWNER, "settings", {"site_projects_per_night": 4}, by="test")
        assert await said() == {"Starts tonight", "Waits its turn: starts in about 1 night"}
        await profile_store.save(session, OWNER, "settings", {"site_projects_per_night": -1}, by="test")
        assert await said() == {"Starts tonight"}


async def test_a_site_with_a_skill_or_an_ended_project_waits_until_the_owner_switches_it_on_again(js):
    async with db.session_factory()() as session:
        await _ready(session)
        known = await _employer(session, "Known", careers="https://known.example.test/jobs", system=SYSTEM_A,
                                board={"slug": "known"}, watched=True)
        sites = await _on_sites(session, 2)  # two more, with no system
        assert known.id and len(sites) == 3
        scout = await _mission(session, missions.SCOUT)
        entry = await missions.start(session, OWNER, scout, by="you")
        by_url = await _sites(session)
        known_site = by_url["https://known.example.test/jobs"]
        projects_made = {p.site_id: p for p in await session.scalars(select(Project))}
        assert known_site.id not in projects_made, "it works already: nothing to learn"
        assert len(entry.started["site_projects"]) == 2
        # one project ends with no skill: it is stopped, and is not started again by itself
        site = by_url["https://e0.example.test/jobs"]
        await escalation.stop_for_good(session, projects_made[site.id])
        assert await _state(session, site) == "stopped"
        [row] = await where_view.listing(session, OWNER, site.id)
        assert row["needs"] == where.ENDED_WORDS
        assert (await autostart.ensure_projects(session, OWNER, by_switch=True)) == []
        # a working site whose skill breaks has never had a project: tonight it gets its first
        await skills.retire_skill(session, await skills.current_skill(session, known_site.id))
        assert await _state(session, known_site) == "waiting"
        [first] = await autostart.ensure_projects(session, OWNER, by_switch=True)
        assert first.site_id == known_site.id
        # and when that project ends with nothing to show, a broken skill is not a reason; only the owner's switch is
        await escalation.stop_for_good(session, first)
        assert (await autostart.ensure_projects(session, OWNER, by_switch=True)) == []
        # off and on again is the owner asking to try once more: tonight it gets a fresh project, not now
        await where.switch(session, OWNER, site.id, False)
        assert await _state(session, site) == "off"
        await where.switch(session, OWNER, site.id, True)
        assert await _state(session, site) == "waiting"
        assert len((await session.scalars(select(Project).where(Project.site_id == site.id))).all()) == 1
        [fresh] = await autostart.ensure_projects(session, OWNER, by_switch=True)
        assert fresh.site_id == site.id
        assert await _state(session, site) == "learning"


async def test_the_schedule_wakes_the_scout_for_each_new_project_after_the_commit(js, knocks, monkeypatch):
    # 3 AM: inside the owner's off hours, where a scout night starts; the pass reads the app's clock, not the test's.
    pin_clock(monkeypatch, datetime(2026, 11, 4, 3, 0, tzinfo=UTC))
    async with db.session_factory()() as session:
        await _ready(session)
        await _on_sites(session, 2)
        scout = await _mission(session, missions.SCOUT)
        await missions.resume(session, scout, by="Tester")
        await session.commit()

    async def due(session, mission, now=None):
        return mission.key == missions.SCOUT

    monkeypatch.setattr(missions, "due", due)
    monkeypatch.setattr(settings, "owner", OWNER)
    woken = await schedule_pass()
    async with db.session_factory()() as session:
        ids = {str(p.id) for p in await session.scalars(select(Project))}
    assert len(ids) == 2 and ids <= set(woken)
    assert {(k, r) for k, r in knocks if k in ids} == {(i, "site_project") for i in ids}
    assert any(r == "search_started" for _k, r in knocks), "and the search is knocked for as before"
    # the wakes come from the data the mission left, one knock each; "waiting" is never taken for a project
    knocks.clear()
    await send_knocks([("waiting", "site_project"), ("some-run", "search_started")])
    assert knocks == [("waiting", "site_project"), ("some-run", "search_started")]


async def test_work_that_waits_for_a_site_project_wakes_the_runner_between_sessions(js):
    async with db.session_factory()() as session:
        await _ready(session)
        await _project(session)
        await session.commit()
    async with db.session_factory()() as session:
        # a project's plan waits and nothing else does: the pass knocks for it instead of failing
        assert await missions.schedule_pass(session, OWNER) == [("waiting", "site_project")]


# --- a project held by a failure is not offered as work -------------------------------------------------------------


async def _project(session, url="https://held.example.test/jobs") -> Project:
    site = await where.add(session, OWNER, url, "job_board")
    return await projects.create_project(session, OWNER, site.id, "Read it", "Its listings can be read",
                                         created_by="you")


async def test_a_project_held_by_a_failure_is_not_offered_until_the_owner_decides(js):
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await load_profile(session, OWNER)
        held, fine = await _project(session), await _project(session, "https://fine.example.test/jobs")
        assert {w["project"].id for w in await site_tools.waiting_projects(session, OWNER)} == {held.id, fine.id}
        await failures.held(session, OWNER, round_process.KEY, "take a plan", RuntimeError("boom"),
                            round_process.subject_of(OWNER, held.id))
        assert [w["project"].id for w in await site_tools.waiting_projects(session, OWNER)] == [fine.id]
        reply = await site_tools.work_next(session, OWNER)
        assert [p["project_id"] for p in reply["projects"]] == [str(fine.id)]
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.SITE
        # the only one left is held: nothing is waiting, so no session is started for it
        await failures.held(session, OWNER, round_process.KEY, "take a plan", RuntimeError("boom"),
                            round_process.subject_of(OWNER, fine.id))
        assert await site_tools.free(session, OWNER) == []
        assert await session_kinds.next_kind(session, OWNER) is None
        assert (await site_tools.work_next(session, OWNER))["projects"] == []
        # Try again closes its task; the project is offered again
        task = await session.scalar(select(Task).where(Task.kind == failures.KIND, Task.link_id == held.id))
        task.state = "done"
        await session.flush()
        assert [w["project"].id for w in await site_tools.waiting_projects(session, OWNER)] == [held.id]


# --- what the owner sees --------------------------------------------------------------------------------------------


async def test_the_listing_says_where_each_site_stands(js):
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        acme = await _employer(session, "Acme", careers="https://acme.example.test/jobs", system=SYSTEM_A,
                               board={"slug": "acme"}, watched=True)
        session.add(Opportunity(owner=OWNER, slug="a1", company="Acme", title="Lead", match_key="acme|lead",
                                employer_id=acme.id))
        session.add(Opportunity(owner=OWNER, slug="a2", company="Acme", title="Head", match_key="acme|head",
                                employer_id=acme.id, screened_out_reason="the place"))
        await where.seed_employer_sites(session, OWNER)
        acme_site = (await _sites(session))["https://acme.example.test/jobs"]
        [row] = await where_view.listing(session, OWNER, acme_site.id)
        # roles are the ones the site's own postings became (test_where_sources.py), not every role at the employer
        assert row == {"id": str(acme_site.id), "url": "https://acme.example.test/jobs", "kind": "employer",
                       "label": "Acme", "on": True, "employer_id": str(acme.id), "state": "working",
                       "status": "Read nightly", "skill": {"version": 1, "kind": "adapter"}, "needs": None, "roles": 0,
                       "added_by": "app", "list": "search", "read_by": "app", "read_found": None, "read_note": None,
                       "off_by": None, "off_note": None, "off_at": None, "by_code": True,
                       "created_at": iso_utc(acme_site.created_at)}
        board = await where.add(session, OWNER, "https://board.example.test/jobs", "job_board")
        assert await _state(session, board) == "waiting"
        learning = await projects.create_project(session, OWNER, board.id, "Read it", "Read it", created_by="you")
        assert await _state(session, board) == "learning"
        # something for the owner: a question from the project, then a held round
        session.add(Task(owner=OWNER, key="ask-1", title="Should it read the second page too?", kind="decision",
                         link_kind="project", link_id=learning.id))
        await session.flush()
        [row] = await where_view.listing(session, OWNER, board.id)
        assert (row["state"], row["needs"]) == ("needs_you", "Should it read the second page too?")
        # switched off, it is only off; a paused project is stopped, with what to do
        await where.switch(session, OWNER, board.id, False)
        assert await _state(session, board) == "off"
        await where.switch(session, OWNER, board.id, True)
        task = await session.scalar(select(Task).where(Task.key == "ask-1"))
        task.state = "done"
        await completion.stop_project(session, learning, "later")
        [row] = await where_view.listing(session, OWNER, board.id)
        assert (row["state"], row["needs"]) == ("stopped", where.PAUSED_WORDS)
        # a skill that works beats everything but a question
        await skills.save_skill(session, OWNER, board.id, {
            "kind": "adapter", "ats": SYSTEM_A, "board": board_of("board")}, learning.id)
        assert await _state(session, board) == "working"
        listed = await where_view.listing(session, OWNER)
        assert [r["label"] for r in listed] == ["Acme", "board.example.test"]
        assert {r["state"] for r in listed} <= set(where.STATES)


async def test_a_job_systems_portal_address_with_a_public_id_in_its_path_is_taken_as_a_site(js):
    key = "0123456789abcdef" * 2
    portal = f"https://www.portal.example.test/v4/ats/web.php/portal/{key}/career-page"
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, portal, "employer")
        assert site.url == portal
