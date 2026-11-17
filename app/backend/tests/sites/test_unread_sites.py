"""Employer sites that are switched on and never read (docs/PRD.md 28.5, 29.3 and 30.3): what Where to look says of
each, and what the night does with a site whose careers page the server is refused or whose employer is on a job
system it has no reader for. A site on a system whose reason for having no reader is known goes to the owner's browser
with the reason on its row (the restricted tier holds those, which name the systems); the Job systems section names as
stuck only the employers nothing reads; a careers page the server is refused (robots.txt, a 403) sends the site to the
browser the same way, as the server's test of a site does, and leaves the night's reads and the learn queue; and a site
handed to the browser keeps the reason it was. Every employer and host is made up (example.test); no network."""


import httpx
import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import actions
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.missions import core as missions
from app.jobsearch.missions import record
from app.sites import autostart, job_systems, projects, read_probe, resolution, skills, where, where_view
from app.sites.models import BY_APP, BY_BROWSER, Site
from tests.sites.builders import UNREAD, board_of
from tests.support import OWNER, load_profile

LIST_PAGE = {"kind": "list_page", "url": "https://learned.example.test/careers", "link_pattern": r"/jobs/\d+"}
REQUEST = {"kind": "request", "method": "GET", "url": "https://asked.example.test/api/jobs", "items": "data",
           "fields": {"title": "name", "url": "path", "ext_id": "id"}, "url_prefix": "https://asked.example.test"}
NOT_SET_UP = "Read in your browser: not set up yet"


async def _world(session) -> None:
    await load_profile(session, OWNER)
    await roster.ensure_roster(session, OWNER)
    await missions.ensure(session, OWNER)


async def _watched(session, name: str, careers: str | None = None, *, system: str | None = None,
                   board: dict | None = None):
    """A watched employer and its site (switched on, because the employer is watched)."""
    employer = await employers.find_or_make(session, OWNER, name)
    employer.careers_url = careers or f"https://{name.lower().replace(' ', '-')}.example.test/jobs"
    employer.check_directly, employer.ats, employer.board = True, system, board or {}
    await session.flush()
    await where.seed_employer_sites(session, OWNER)
    site = (await session.scalars(select(Site).where(Site.employer_id == employer.id))).one()
    return employer, site


async def _row(session, site) -> dict:
    [row] = await where_view.listing(session, OWNER, site.id)
    return row


# --- a site that waits for its project on a system nothing reads ------------------------------------------------------


async def test_a_site_on_a_system_with_no_reader_and_no_reason_known_says_only_when_it_starts(js):
    """This system has no reader because none has been built: that says nothing against the scout learning the site."""
    async with db.session_factory()() as session:
        await _world(session)
        _employer, site = await _watched(session, "Vale", system=UNREAD, board=board_of("vale"))
        scout = await missions.standing(session, OWNER, record.SCOUT)
        assert (await _row(session, site))["status"] == where_view.start_words(scout, 0)


# --- an employer on a system the server has no reader for goes to the browser -----------------------------------------


# --- the Job systems section names only the employers nothing reads ---------------------------------------------------


async def test_a_system_with_no_reader_names_as_stuck_only_the_watched_employers_no_recipe_reads(js):
    async with db.session_factory()() as session:
        await _world(session)
        board = board_of("vale")
        await _watched(session, "Waiting Co", system=UNREAD, board=board)
        _a, learned = await _watched(session, "Learned Co", system=UNREAD, board=board)
        _b, asked = await _watched(session, "Asked Co", system=UNREAD, board=board)
        _c, replaced = await _watched(session, "Replaced Co", system=UNREAD, board=board)
        _d, phone = await _watched(session, "Phone Co", system=UNREAD, board=board)
        await skills.save_skill(session, OWNER, learned.id, LIST_PAGE, None)
        await skills.save_skill(session, OWNER, asked.id, REQUEST, None)
        # a recipe that was retired reads nothing: the newest one in force is what counts
        old = await skills.save_skill(session, OWNER, replaced.id, LIST_PAGE, None)
        await skills.retire_skill(session, old)
        await where.set_read_by(session, OWNER, phone.id, BY_BROWSER)
        [unread] = [r for r in await job_systems.listing(session, OWNER) if r["key"] == UNREAD]
        assert [n["name"] for n in unread["employer_names"]] == ["Replaced Co", "Waiting Co"], (
            "Learned Co and Asked Co are read every night, and Phone Co is the owner's browser's")
        # and the sites to search say that the two are read
        assert {r["label"]: r["state"] for r in await where_view.listing(session, OWNER)} == {
            "Waiting Co": "waiting", "Learned Co": "working", "Asked Co": "working", "Replaced Co": "waiting",
            "Phone Co": "browser"}


# --- a careers page the server is refused -----------------------------------------------------------------------------


def _internet(monkeypatch, seen: list[str]) -> None:
    """Every careers page answers: blocked.example.test with a 403, rules.example.test with a robots.txt that asks
    not to read /search, and the rest with a page that names no job system."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.path == "/robots.txt":
            if request.url.host == "rules.example.test":
                return httpx.Response(200, text="User-agent: *\nDisallow: /search\n")
            return httpx.Response(404)
        if request.url.host == "blocked.example.test":
            return httpx.Response(403)
        return httpx.Response(200, text="<html>Email us your resume.</html>")

    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0))


async def test_a_careers_page_the_server_is_refused_sends_the_site_to_the_browser_with_the_reason(js, monkeypatch):
    seen: list[str] = []
    _internet(monkeypatch, seen)
    async with db.session_factory()() as session:
        await _world(session)
        _a, blocked = await _watched(session, "Blocked Co", "https://blocked.example.test/careers")
        _b, rules = await _watched(session, "Rules Co", "https://rules.example.test/search/jobs")
        _c, plain = await _watched(session, "Plain Co", "https://plain.example.test/careers")
        assert [e.name for e in await where.watched_employers(session, OWNER)] == ["Blocked Co", "Plain Co", "Rules Co"]
        await actions.resolve_links(session, None, {"owner": OWNER})
        # the two the server was refused are read in the owner's browser, each with the reason it was, and the row says
        # so: nothing about them waits for a project
        for site, reason in ((blocked, "It turns apps away."), (rules, "Its robots rules turn apps away.")):
            row = await _row(session, site)
            assert (row["state"], row["status"], row["on"]) == ("browser", NOT_SET_UP, True)
            assert (row["read_by"], row["read_note"]) == ("browser", reason)
        # the one that answered and named no system still waits for its project, with no reason of the server's
        row = await _row(session, plain)
        assert (row["state"], row["read_by"], row["read_note"]) == ("waiting", "app", None)
        # they leave the night's read and the learn queue, and the employer says what happened
        assert [e.name for e in await where.watched_employers(session, OWNER)] == ["Plain Co"]
        assert [s.id for s in await session.scalars(autostart.start_queue(OWNER))] == [plain.id]
        employer = await employers.find(session, OWNER, "Blocked Co")
        assert employer.fetch_kind == "refused"
        assert employer.fetch_note == "careers page: couldn't be read (blocked.example.test refused the request (403))"
        # the next night asks neither of them anything, and a site the owner puts back to the server is not handed over
        # again: it was refused once and that is on the employer
        seen.clear()
        await where.set_read_by(session, OWNER, blocked.id, BY_APP)
        await actions.resolve_links(session, None, {"owner": OWNER})
        assert "blocked.example.test" not in seen and "rules.example.test" not in seen
        assert (await _row(session, blocked))["read_by"] == "app"


async def test_a_site_is_handed_over_only_while_it_is_on_and_read_by_the_app(js):
    async with db.session_factory()() as session:
        await _world(session)
        sites = {}
        refused = {}
        for name in ("Mine", "Off", "Browser", "Found open", "Found closed"):
            employer, sites[name] = await _watched(session, name)
            refused[employer.id] = "site"
        await where.switch(session, OWNER, sites["Off"].id, False)
        await where.set_read_by(session, OWNER, sites["Browser"].id, BY_BROWSER)
        sites["Found open"].read_by, sites["Found open"].read_found = "auto", "app"
        sites["Found closed"].read_by, sites["Found closed"].read_found = "auto", "browser"
        await session.flush()
        stranger = await employers.find_or_make(session, OWNER, "Stranger")  # a refused employer with no site at all
        refused[stranger.id] = "robots"
        assert await read_probe.hand_refused(session, OWNER, {}) == 0
        assert await read_probe.hand_refused(session, OWNER, refused) == 2
        assert (sites["Mine"].read_by, sites["Mine"].read_note) == (BY_BROWSER, read_probe.TURNED_AWAY_NOTE)
        assert (sites["Found open"].read_by, sites["Found open"].read_found, sites["Found open"].read_note) == (
            "auto", BY_BROWSER, read_probe.TURNED_AWAY_NOTE), "a site left to resumebot is found to need the browser"
        assert (sites["Off"].read_by, sites["Off"].read_note) == (BY_APP, None), "a switch that is off is left off"
        assert (sites["Browser"].read_by, sites["Browser"].read_note) == (BY_BROWSER, None), "the owner's own choice"
        assert sites["Found closed"].read_note is None, "already read there"


async def test_the_robots_sentence_is_the_one_the_servers_test_of_a_site_gives(js):
    async with db.session_factory()() as session:
        await _world(session)
        employer, site = await _watched(session, "Rules Co")
        assert await read_probe.hand_refused(session, OWNER, {employer.id: "robots"}) == 1
        assert site.read_note == read_probe.ROBOTS_NOTE == "Its robots rules turn apps away."


# --- the night's attempt to complete a board address ------------------------------------------------------------------


# --- a site handed to the browser keeps the reason it was -------------------------------------------------------------


async def test_a_site_handed_to_the_browser_keeps_the_reason_and_the_row_says_it(js):
    async with db.session_factory()() as session:
        await _world(session)
        _employer, site = await _watched(session, "Phone Co")
        why = "Its listings only load in a browser."
        assert await where.hand_to_browser(session, site, why) is True
        assert (site.read_by, site.read_found, site.read_note) == (BY_BROWSER, None, why)
        row = await _row(session, site)
        assert (row["state"], row["read_note"]) == ("browser", why)
        # a site the owner chose to read there has no reason to give; changing it starts over
        await where.set_read_by(session, OWNER, site.id, BY_APP)
        assert (await _row(session, site))["read_note"] is None
        await where.set_read_by(session, OWNER, site.id, BY_BROWSER)
        assert (await _row(session, site))["read_note"] is None
        # and one read there already is not handed over again
        assert await where.hand_to_browser(session, site, "it asks for a sign-in") is False


async def test_a_site_handed_over_before_its_row_kept_the_reason_says_what_its_project_ended_with(js):
    outcome = ("Handed to your browser: site returns 403 to all fetches, even robots.txt, likely a bot block, so the "
               "app can't read it from the server.")
    async with db.session_factory()() as session:
        await _world(session)
        _employer, site = await _watched(session, "Old Phone Co")
        project = await projects.create_project(session, OWNER, site.id, "Read it", "Read it", created_by="you")
        project.resolution = {"problem": "A site to learn.", "done": "Nothing was changed.", "outcome": outcome}
        site.read_by = BY_BROWSER
        await session.flush()
        assert site.read_note is None
        row = await _row(session, site)
        assert row["read_note"] == "Site returns 403 to all fetches, even robots.txt, likely a bot block."
        # a site the app reads, or a project that ended another way, has nothing to say
        site.read_by = BY_APP
        assert (await _row(session, site))["read_note"] is None
        site.read_by = BY_BROWSER
        project.resolution = {**project.resolution, "outcome": "Resolved: the last check confirmed it."}
        assert (await _row(session, site))["read_note"] is None


@pytest.mark.parametrize(("outcome", "reason"), [
    ("Handed to your browser: its listings only load in a browser, so the app can't read it from the server.",
     "Its listings only load in a browser."),
    ("Handed to your browser: the server cannot read it, so the app can't read it from the server",
     "The server cannot read it."),
    ("Resolved: the last check confirmed it.", None),
    ("Stopped: it can't be read (it asks not to be read), so resumebot switched the site off.", None),
    ("Handed to your browser: , so the app can't read it from the server.", None),
    ("", None), (None, None), (7, None),
])
def test_the_reason_a_project_gave_for_handing_a_site_over_is_read_back_from_its_outcome(outcome, reason):
    assert resolution.browser_reason(outcome) == reason


async def test_the_outcome_the_app_writes_for_a_site_handed_over_is_one_it_reads_back(js):
    async with db.session_factory()() as session:
        await _world(session)
        _employer, site = await _watched(session, "Round Trip Co")
        project = await projects.create_project(session, OWNER, site.id, "Read it", "Read it", created_by="you")
        written = await resolution.build(session, project, resolution.TO_BROWSER, "it asks for a sign-in")
        assert resolution.browser_reason(written["outcome"]) == "It asks for a sign-in."
        long = await resolution.build(session, project, resolution.TO_BROWSER, where.clause_of("a reason " * 40))
        assert resolution.browser_reason(long["outcome"]) is not None, "the longest reason still reads back"
