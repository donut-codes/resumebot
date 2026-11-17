"""What Where to look's Status column says (docs/PRD.md 30.3), from the site's state and the missions behind it: a site
with a working skill is read at the time of "Read the employers' boards" (or says that mission is paused), one waiting
for its project starts tonight only while "Scout the boards" is on, one read in the owner's browser says whether that
mission has ever run. Every site is made up (example.test); no network."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Mission, MissionRun
from app.models.work import Task
from app.settings import settings
from app.sites import autostart, completion, projects, skills, where, where_view
from app.timeutil import iso_utc
from tests.support import EXAMPLE_SYSTEM, OWNER, clock_at, learn_sites, load_profile

RECIPE = {"kind": "adapter", "ats": EXAMPLE_SYSTEM, "board": {"slug": "board"}}


async def _mission(session, key: str) -> Mission:
    await load_profile(session, OWNER)
    await roster.ensure_roster(session, OWNER)
    await missions.ensure(session, OWNER)
    return await session.scalar(select(Mission).where(Mission.owner == OWNER, Mission.key == key))


async def _status(session, site) -> str:
    [row] = await where_view.listing(session, OWNER, site.id)
    return row["status"]


async def _working(session, url: str = "https://working.example.test/jobs"):
    """A site switched on that has a skill that works."""
    site = await where.add(session, OWNER, url)
    await skills.save_skill(session, OWNER, site.id, RECIPE, None)
    return site


async def test_a_site_with_a_working_skill_is_read_at_the_time_of_the_mission_that_reads_the_boards(js):
    async with db.session_factory()() as session:
        read = await _mission(session, missions.READ)
        assert read.name == "Read the employers' boards" and read.schedule == {"kind": "daily", "at": "01:00"}
        site = await _working(session)
        assert await _status(session, site) == "Read nightly at 1:00 AM"
        for at, words in (("23:05", "Read nightly at 11:05 PM"), ("05:59", "Read nightly at 5:59 AM"),
                          ("12:00", "Read every day at 12:00 PM"), ("14:05", "Read every day at 2:05 PM")):
            read.schedule = {"kind": "daily", "at": at}
            assert await _status(session, site) == words
        read.schedule = {"kind": "every", "hours": 6}
        assert await _status(session, site) == "Read every 6 hours"
        read.schedule = {"kind": "on_demand"}
        assert await _status(session, site) == "Read only when you run it"
        read.schedule = {"kind": "daily", "at": "nonsense"}
        assert await _status(session, site) == "Read every day at nonsense"


async def test_a_working_site_says_so_when_the_mission_that_reads_the_boards_is_paused(js):
    async with db.session_factory()() as session:
        read = await _mission(session, missions.READ)
        site = await _working(session)
        await missions.pause(session, read, by="Tester")
        assert await _status(session, site) == "Waits: the reading mission is paused"
        await missions.resume(session, read, by="Tester")
        assert await _status(session, site) == "Read nightly at 1:00 AM"


async def test_a_site_with_no_missions_yet_still_says_how_it_stands(js):
    async with db.session_factory()() as session:
        site = await _working(session)
        assert await _status(session, site) == "Read nightly"
        waiting = await where.add(session, OWNER, "https://waiting.example.test/jobs")
        assert await _status(session, waiting) == "Starts tonight"
        phone = await where.add(session, OWNER, "https://phone.example.test/jobs", read_by="browser")
        assert await _status(session, phone) == "Read in your browser: not set up yet"


async def test_a_site_waiting_for_its_project_starts_tonight_only_while_the_scout_is_on(js):
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        site = await where.add(session, OWNER, "https://waiting.example.test/jobs")
        await missions.resume(session, scout, by="Tester")
        assert await _status(session, site) == "Starts tonight at 1:00 AM"
        await missions.pause(session, scout, by="Tester")
        assert await _status(session, site) == "Waits: the scouting mission is paused"
        # a switch that is off says nothing, whatever the mission is doing
        await where.switch(session, OWNER, site.id, False)
        assert await _status(session, site) == ""


async def _waiting(session, count: int) -> list:
    """`count` sites switched on and waiting for a project, the first made the last to have been switched on."""
    sites = []
    for i in range(count):
        site = await where.add(session, OWNER, f"https://board{i}.example.test/jobs")
        site.turned_on_at = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(hours=count - i)
        sites.append(site)
    await session.flush()
    return sites


async def test_only_the_sites_the_scout_starts_tonight_say_starts_tonight_and_the_rest_say_their_turn(js):
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        await missions.resume(session, scout, by="Tester")
        assert scout.schedule == {"kind": "daily", "at": "01:00"}
        sites = await _waiting(session, 5)
        said = {s.url: await _status(session, s) for s in sites}
        assert [said[s.url] for s in reversed(sites)] == [
            "Starts tonight at 1:00 AM", "Starts tonight at 1:00 AM", "Starts tonight at 1:00 AM",
            "Waits its turn: starts in about 1 night", "Waits its turn: starts in about 1 night"]
        # the whole list says the same as one site asked for alone
        assert {r["url"]: r["status"] for r in await where_view.listing(session, OWNER)} == said
        scout.schedule = {"kind": "daily", "at": "23:30"}
        assert await _status(session, sites[-1]) == "Starts tonight at 11:30 PM"


async def test_a_seventh_waiting_site_waits_about_two_nights_and_a_night_is_three_sites(js):
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        await missions.resume(session, scout, by="Tester")
        sites = await _waiting(session, 9)
        said = [await _status(session, s) for s in reversed(sites)]
        nights = {"Starts tonight at 1:00 AM": 0, "Waits its turn: starts in about 1 night": 1,
                  "Waits its turn: starts in about 2 nights": 2}
        assert [nights[w] for w in said] == [0, 0, 0, 1, 1, 1, 2, 2, 2]
        assert said[6] == "Waits its turn: starts in about 2 nights"


async def test_the_sites_that_say_starts_tonight_are_the_ones_the_mission_starts_in_its_own_order(js):
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        await missions.resume(session, scout, by="Tester")
        sites = await _waiting(session, 6)
        sites[2].turned_on_at = None  # switched on with no time kept: the mission takes it last
        await session.flush()
        tonight = {s.id for s in sites if (await _status(session, s)).startswith("Starts tonight")}
        assert len(tonight) == missions.SITE_PROJECTS_A_NIGHT
        assert sites[2].id not in tonight
        made = await autostart.ensure_projects(session, OWNER, limit=missions.SITE_PROJECTS_A_NIGHT, by_switch=True)
        assert {p.site_id for p in made} == tonight
        # once they are learning, the next three are tonight's
        later = {s.id: await _status(session, s) for s in sites if s.id not in tonight}
        assert sum(w.startswith("Starts tonight") for w in later.values()) == 3 and len(later) == 3


async def test_every_waiting_site_keeps_the_paused_wording_while_the_scout_is_paused(js):
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        await missions.pause(session, scout, by="Tester")
        sites = await _waiting(session, 5)
        assert {await _status(session, s) for s in sites} == {"Waits: the scouting mission is paused"}


async def test_the_states_that_wait_on_the_owner_or_the_project_keep_their_own_words(js):
    async with db.session_factory()() as session:
        scout = await _mission(session, missions.SCOUT)
        await missions.pause(session, scout, by="Tester")
        site = await where.add(session, OWNER, "https://board.example.test/jobs")
        project = await projects.create_project(session, OWNER, site.id, "Read it", "Read it", created_by="you")
        assert await _status(session, site) == "Learning to read it", "a project already open is not waiting"
        session.add(Task(owner=OWNER, key="ask-1", title="Should it read the second page too?", kind="decision",
                         link_kind="project", link_id=project.id))
        await session.flush()
        assert await _status(session, site) == "Needs you"
        task = await session.scalar(select(Task).where(Task.key == "ask-1"))
        task.state = "done"
        await completion.stop_project(session, project, "later")
        assert await _status(session, site) == "Stopped"


async def test_a_site_read_in_the_browser_says_whether_that_mission_has_ever_run_and_when_it_last_did(js, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")
    # one "now" for the runs' dates and for the year the words leave out: the same year in both zones asserted in below
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(where_view, "datetime", clock_at(now))
    async with db.session_factory()() as session:
        browser = await _mission(session, missions.BROWSER)
        site = await where.add(session, OWNER, "https://phone.example.test/jobs", read_by="browser")
        assert await _status(session, site) == "Read in your browser: not set up yet"
        await learn_sites(session, site)  # a recipe a learn run kept

        async def ran(when: datetime) -> None:
            session.add(MissionRun(owner=OWNER, mission_id=browser.id, started_by="browser", outcome="done",
                                   started_at=when))
            await session.flush()

        await ran(datetime(2020, 1, 6, 3, 0, tzinfo=UTC))
        assert await _status(session, site) == "Read in your browser: last run Jan 6, 2020"
        # the newest run is the one that counts, and a run this year leaves the year out
        this_year = now.year
        await ran(datetime(this_year, 1, 5, 12, 0, tzinfo=UTC))
        assert await _status(session, site) == "Read in your browser: last run Jan 5"
        # the day is the install's own: two in the morning in UTC is still the evening before in New York
        await ran(datetime(this_year, 6, 2, 2, 0, tzinfo=UTC))
        assert await _status(session, site) == "Read in your browser: last run Jun 2"
        monkeypatch.setattr(settings, "timezone", "America/New_York")
        assert await _status(session, site) == "Read in your browser: last run Jun 1"


async def test_a_site_read_in_the_browser_says_whether_it_is_waiting_to_be_learned_being_learned_or_off(js):
    """docs/PRD.md 37.8: waiting (no recipe, no attempt yet), learning (an attempt failed, or its recipe stopped
    fitting: the note), read (it has a recipe), and off when the app could not learn it."""
    async with db.session_factory()() as session:
        browser = await _mission(session, missions.BROWSER)
        site = await where.add(session, OWNER, "https://phone.example.test/jobs", read_by="browser")
        assert await _status(session, site) == "Read in your browser: not set up yet", "the mission never ran"
        session.add(MissionRun(owner=OWNER, mission_id=browser.id, started_by="browser", outcome="done",
                               started_at=datetime(2020, 1, 6, 3, 0, tzinfo=UTC)))
        await session.flush()
        assert await _status(session, site) == "Read in your browser: waiting to be learned"
        site.learn_note = "the search needs a Search button pressed"
        assert await _status(session, site) == "Learning in your browser: the search needs a Search button pressed"
        await learn_sites(session, site)
        site.learn_note = None
        assert (await _status(session, site)).startswith("Read in your browser: last run ")
        # the app gave up: its own words, once, not doubled with the old ones
        site.learn_note = "the results never load"
        await where.turn_off_unlearnable(session, site, "the results never load")
        [row] = await where_view.listing(session, OWNER, site.id)
        assert row["state"] == "off" and row["off_by"] == "app"
        assert row["off_note"] == "couldn't learn how to read it in your browser (the results never load)"
        assert row["status"] == "Off: couldn't learn how to read it in your browser (the results never load)"


async def test_the_page_carries_the_status_and_the_day_a_site_was_added(client):
    async with db.session_factory()() as session:
        read = await _mission(session, missions.READ)
        site = await _working(session)
        added = iso_utc(site.created_at)
        await missions.pause(session, read, by="Tester")
        await session.commit()
    rows = (await client.get("/api/v1/sites")).json()["sites"]
    [row] = [r for r in rows if r["url"] == "https://working.example.test/jobs"]
    assert row["status"] == "Waits: the reading mission is paused"
    assert row["created_at"] == added and row["created_at"].endswith("+00:00")
    off = await client.patch(f"/api/v1/sites/{row['id']}", json={"on": False})
    assert off.status_code == 200 and (off.json()["status"], off.json()["created_at"]) == ("", added)
    on = await client.patch(f"/api/v1/sites/{row['id']}", json={"on": True})
    assert on.json()["status"] == "Waits: the reading mission is paused"
