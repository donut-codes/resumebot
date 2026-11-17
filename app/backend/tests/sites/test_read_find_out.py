"""A site can be left to resumebot to test (docs/PRD.md 30.3): the server tests it once (code, no Claude), the app reads
it if that works and the owner's browser does if it does not, whatever the reason; the one rule for who reads a site
(`effective_read`) that the night's read, the site projects and the browser reader all ask; and what happens when the
browser reader cannot read a site (it is switched off and the owner told once) or a run ends before it read one (one
Inbox item). Every site is made up (example.test) and answers through a mock
transport; no network."""

import asyncio
from collections.abc import Callable
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
from app.jobsearch.models import Employer, JobPosting
from app.jobsearch.postings import index as postings
from app.sites import autostart, projects, read_probe, where, where_view
from app.sites.base import PlanEngineError
from app.sites.models import AUTO, BY_APP, BY_BROWSER, READ_BYS, Site, effective_read, read_is
from tests.support import EXAMPLE_SYSTEM, OPEN_PAGE, OWNER, load_profile, open_internet

SITES = "/api/v1/sites"
NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
HOST = "board.example.test"
URL = f"https://{HOST}/jobs"
OPEN = read_probe.OPEN_NOTE
NOT_SET_UP = "Read in your browser: not set up yet"
BROWSER = BY_BROWSER


# --- the made-up internet ------------------------------------------------------------------------------------------


def fetcher(handle) -> Fetcher:
    """A polite fetcher as production makes it (it follows redirects), answering through `handle`."""
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True), per_host_seconds=0)


def served(page: Callable[[httpx.Request], httpx.Response], robots: tuple[int, str] | Exception | None = None):
    """A site's handler: its robots.txt as given (none: 404; an exception is raised), every other address by `page`."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            if isinstance(robots, Exception):
                raise robots
            return httpx.Response(404) if robots is None else httpx.Response(robots[0], text=robots[1])
        return page(request)

    return handle


def site_says(monkeypatch, handle) -> None:
    """Every site the server tests answers as `handle` says."""
    monkeypatch.setattr(read_probe, "make_fetcher", lambda: fetcher(handle))


def status(code: int, **headers):
    return lambda request: httpx.Response(code, headers=headers)


def html(body: str, title: str = "Careers"):
    page = f"<html><head><title>{title}</title></head><body>{body}</body></html>"
    return lambda request: httpx.Response(200, html=page)


def redirect(to: str, then: Callable[[httpx.Request], httpx.Response]):
    """The site's /jobs sends the visitor to `to`; every other address is `then`."""

    def page(request: httpx.Request) -> httpx.Response:
        if request.url.host == HOST and request.url.path == "/jobs":
            return httpx.Response(302, headers={"location": to})
        return then(request)

    return page


#: Enough text that a page is a listing, not a wall; a sign-in form; and the parts of a sign-in wall's page.
LONG = "<p>" + "Open roles, each with its place, its pay and what the work is. " * 40 + "</p>"
LOGIN = '<form action="/session"><input name="email"><input type="password" name="password"><button>Go</button></form>'
LISTING = html(LONG)


def _broken(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("no answer", request=request)


# --- the server's test ----------------------------------------------------------------------------------------------

PROBES = [
    pytest.param(LISTING, None, (BY_APP, OPEN), id="a page with content is open to the app"),
    pytest.param(lambda r: httpx.Response(200, json={"jobs": [{"id": 1}]}), None, (BY_APP, OPEN), id="a feed is open"),
    pytest.param(html(LONG + '<p><a href="/login">Sign in</a> to save jobs.</p>'), None, (BY_APP, OPEN),
                 id="a listing that only offers a sign-in link is open"),
    pytest.param(html(LOGIN + LONG), None, (BY_APP, OPEN), id="a listing with a sign-in box on it is open"),
    pytest.param(html(LONG + "<p>Protected by a captcha on forms.</p>"), None, (BY_APP, OPEN),
                 id="a long page that mentions a captcha is open"),
    pytest.param(redirect("/en/jobs", LISTING), None, (BY_APP, OPEN), id="a redirect to another page of it is open"),
    pytest.param(LISTING, (200, "User-agent: *\nDisallow: /private"), (BY_APP, OPEN), id="robots rules for elsewhere"),
    pytest.param(LISTING, (200, "User-agent: *\nDisallow: /"), (BROWSER, read_probe.ROBOTS_NOTE),
                 id="robots rules turn apps away"),
    pytest.param(status(401), None, (BROWSER, read_probe.SIGN_IN_NOTE), id="401 asks for a sign-in"),
    pytest.param(status(407), None, (BROWSER, read_probe.SIGN_IN_NOTE), id="407 asks for a sign-in"),
    pytest.param(status(403), None, (BROWSER, read_probe.TURNED_AWAY_NOTE), id="403 turns apps away"),
    pytest.param(status(429, **{"Retry-After": "30"}), None, (BROWSER, read_probe.TURNED_AWAY_NOTE),
                 id="429 turns apps away, and the wait is not sat out"),
    pytest.param(status(999), None, (BROWSER, read_probe.TURNED_AWAY_NOTE), id="999 turns apps away"),
    pytest.param(redirect("/login?next=%2Fjobs", LISTING), None, (BROWSER, read_probe.SIGN_IN_NOTE),
                 id="a redirect to /login is a sign-in wall"),
    pytest.param(redirect("/users/sign_in", LISTING), None, (BROWSER, read_probe.SIGN_IN_NOTE),
                 id="a redirect to a sign_in path is a sign-in wall"),
    pytest.param(redirect("https://login.example.test/", LISTING), None, (BROWSER, read_probe.SIGN_IN_NOTE),
                 id="a redirect to a sign-in host is a sign-in wall"),
    pytest.param(html(LONG, title="Sign in | Example"), None, (BROWSER, read_probe.SIGN_IN_NOTE),
                 id="a page titled as a sign-in page is a wall"),
    pytest.param(html(LOGIN, title="Welcome"), None, (BROWSER, read_probe.SIGN_IN_NOTE),
                 id="a password field and little else is a wall"),
    pytest.param(html("<h1>Access Denied</h1><p>Reference 18.abc</p>", title="Access Denied"), None,
                 (BROWSER, read_probe.TURNED_AWAY_NOTE), id="a short page that says access is denied"),
    pytest.param(html("<p>Verify you are human to continue.</p>", title="Hold on"), None,
                 (BROWSER, read_probe.TURNED_AWAY_NOTE), id="a short bot check"),
    pytest.param(status(503), None, (BROWSER, read_probe.NO_ANSWER_NOTE), id="a failing server goes to the browser"),
    pytest.param(status(404), None, (BROWSER, "It answered the server with 404."),
                 id="a page that is not there goes to the browser"),
    pytest.param(lambda r: httpx.Response(200, html="  \n "), None, (BROWSER, read_probe.EMPTY_NOTE),
                 id="an empty page goes to the browser"),
    pytest.param(_broken, None, (BROWSER, read_probe.NO_ANSWER_NOTE), id="no answer in time goes to the browser"),
    pytest.param(LISTING, (500, "oops"), (BROWSER, read_probe.NO_ANSWER_NOTE),
                 id="robots rules that fail go to the browser, without blaming the robots rules"),
    pytest.param(LISTING, httpx.ConnectError("down"), (BROWSER, read_probe.NO_ANSWER_NOTE),
                 id="robots rules that cannot be fetched go to the browser"),
]


@pytest.mark.parametrize("page, robots, expected", PROBES)
async def test_the_server_test_says_from_the_answer_how_a_site_is_read(page, robots, expected, monkeypatch):
    monkeypatch.setattr(read_probe, "PROBE_SECONDS", 5.0)  # a test that waits when it should not fails, not hangs
    fetch = fetcher(served(page, robots))
    try:
        found = await read_probe.probe(URL, fetch)
    finally:
        await fetch.close()
    assert (found.read, found.note) == expected


async def test_the_test_never_takes_longer_than_its_limit(monkeypatch):
    assert read_probe.PROBE_SECONDS <= 15
    monkeypatch.setattr(read_probe, "PROBE_SECONDS", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return httpx.Response(200, html=OPEN_PAGE)

    fetch = fetcher(slow)
    try:
        found = await asyncio.wait_for(read_probe.probe(URL, fetch), 1.5)
    finally:
        await fetch.close()
    assert (found.read, found.note) == (BROWSER, read_probe.NO_ANSWER_NOTE)


async def test_the_polite_fetcher_says_when_robots_could_not_be_read_and_can_decline_to_wait():
    down = fetcher(served(LISTING, (500, "oops")))
    with pytest.raises(polite.RobotsRefused) as failing:
        await down.get(URL)
    assert failing.value.unreachable is True and isinstance(failing.value, polite.Refused)
    shut = fetcher(served(LISTING, (200, "User-agent: *\nDisallow: /")))
    with pytest.raises(polite.RobotsRefused) as saying:
        await shut.get(URL)
    assert saying.value.unreachable is False
    # the night's read classes a robots.txt that says no as a refusal, and one that could not be read as the site being
    # down tonight (a refusal retires a skill that works)
    assert postings.fault_kind(saying.value) == "refused" and postings.fault_kind(failing.value) == "network"
    # "too many requests" comes back at once when the caller is not patient, not after the wait the site asked for
    busy = fetcher(served(status(429, **{"Retry-After": "30"})))
    reply = await asyncio.wait_for(busy.get(URL, patient=False), 3)
    assert reply.status_code == 429


# --- adding a site, or switching it to "ask resumebot to find out" -----------------------------------------------


@pytest.mark.parametrize("page, found, note, state, status_words", [
    (LISTING, "app", OPEN, "waiting", "Starts tonight"),
    (status(401), "browser", read_probe.SIGN_IN_NOTE, "browser", NOT_SET_UP),
    (_broken, "browser", read_probe.NO_ANSWER_NOTE, "browser", NOT_SET_UP),
])
async def test_a_site_added_with_no_choice_is_tested_once_and_the_row_says_what_was_found(
        client, monkeypatch, page, found, note, state, status_words):
    site_says(monkeypatch, served(page))
    made = await client.post(SITES, json={"url": URL})
    assert made.status_code == 200
    row = made.json()
    assert (row["read_by"], row["read_found"], row["read_note"]) == ("auto", found, note)
    assert (row["state"], row["status"], row["on"], row["needs"]) == (state, status_words, True, None)


async def test_only_a_site_left_to_resumebot_is_tested_and_only_once(client, monkeypatch):
    tried: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/robots.txt":
            tried.append(f"{request.url.host}{request.url.path}")
            return httpx.Response(401)
        return httpx.Response(404)

    site_says(monkeypatch, handle)
    # the owner chose: no test
    chosen_app = await client.post(SITES, json={"url": "https://chosen-app.example.test/jobs", "read_by": "app"})
    chosen_browser = await client.post(SITES, json={"url": "https://chosen.example.test/jobs", "read_by": "browser"})
    avoided = await client.post(SITES, json={"url": "https://avoided.example.test", "list": "avoid"})
    assert tried == []
    assert (chosen_app.json()["read_found"], chosen_app.json()["state"]) == (None, "waiting")
    assert (chosen_browser.json()["read_found"], chosen_browser.json()["state"]) == (None, "browser")
    assert avoided.status_code == 200 and avoided.json()["list"] == "avoid"
    # switched to "ask resumebot": tested at once, and what it found is on the row
    changed = await client.patch(f"{SITES}/{chosen_app.json()['id']}", json={"read_by": "auto"})
    assert tried == ["chosen-app.example.test/jobs"]
    assert (changed.json()["read_by"], changed.json()["read_found"], changed.json()["state"]) == (
        "auto", "browser", "browser")
    # a switch does not test it again, and the same choice again does not either, nor does adding it again
    await client.patch(f"{SITES}/{chosen_app.json()['id']}", json={"on": False})
    await client.patch(f"{SITES}/{chosen_app.json()['id']}", json={"on": True})
    await client.patch(f"{SITES}/{chosen_app.json()['id']}", json={"read_by": "auto"})
    await client.post(SITES, json={"url": "https://chosen-app.example.test/jobs"})
    assert tried == ["chosen-app.example.test/jobs"]
    # chosen again, it forgets what it found; asked again, it is tested again
    back = await client.patch(f"{SITES}/{chosen_app.json()['id']}", json={"read_by": "app"})
    assert (back.json()["read_by"], back.json()["read_found"], back.json()["read_note"], back.json()["state"]) == (
        "app", None, None, "waiting")
    site_says(monkeypatch, served(LISTING))
    again = await client.patch(f"{SITES}/{chosen_app.json()['id']}", json={"read_by": "auto"})
    assert (again.json()["read_found"], again.json()["state"]) == ("app", "waiting")


async def test_a_site_is_read_one_of_three_ways_and_a_site_added_with_no_choice_is_left_to_resumebot(client):
    assert READ_BYS == ("auto", "app", "browser")
    bad = await client.post(SITES, json={"url": "https://third.example.test/", "read_by": "elsewhere"})
    assert bad.status_code == 422 and "auto, app, browser" in bad.json()["detail"]
    assert (await client.post(SITES, json={"url": "https://fourth.example.test/"})).json()["read_by"] == "auto"


# --- the one rule for who reads a site --------------------------------------------------------------------------------


COMBINATIONS = [(AUTO, None), (AUTO, BY_APP), (AUTO, BY_BROWSER), (BY_APP, None), (BY_BROWSER, None),
                (BY_APP, BY_BROWSER), (BY_BROWSER, BY_APP)]


async def test_who_reads_a_site_is_the_owners_choice_or_for_one_left_to_resumebot_what_was_found(js):
    async with db.session_factory()() as session:
        made: list[Site] = []
        for n, (by, found) in enumerate(COMBINATIONS):
            made.append(Site(owner=OWNER, url=f"https://s{n}.example.test/", kind="job_board", read_by=by,
                             read_found=found, created_at=NOW))
        session.add_all(made)
        await session.flush()
        assert [effective_read(s) for s in made] == [None, BY_APP, BY_BROWSER, BY_APP, BY_BROWSER, BY_APP, BY_BROWSER]
        for read in (BY_APP, BY_BROWSER):  # the query says the same as the function
            in_query = set((await session.scalars(select(Site.id).where(read_is(read)))).all())
            assert in_query == {s.id for s in made if effective_read(s) == read}


async def test_a_site_found_readable_or_found_to_need_the_browser_is_listed_and_read_like_one_chosen(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        sites = {}
        for name, by, found in (("auto-app", AUTO, BY_APP), ("auto-browser", AUTO, BY_BROWSER),
                                ("app", BY_APP, None), ("browser", BY_BROWSER, None)):
            sites[name] = await where.add(session, OWNER, f"https://{name}.example.test/jobs", "job_board", read_by=by)
            sites[name].read_found = found
        untested = await where.add(session, OWNER, "https://untested.example.test/jobs", "job_board", read_by=AUTO)
        await session.flush()
        assert [s.url for s in await where.browser_sites(session, OWNER)] == [
            "https://auto-browser.example.test/jobs", "https://browser.example.test/jobs"], "none for the untested"
        rows = {r["label"]: r for r in await where_view.listing(session, OWNER)}
        states = {name: (rows[f"{name}.example.test"]["state"], rows[f"{name}.example.test"]["status"])
                  for name in sites}
        assert states == {
            "auto-app": ("waiting", "Starts tonight"),
            "auto-browser": ("browser", NOT_SET_UP),
            "app": ("waiting", "Starts tonight"),
            "browser": ("browser", NOT_SET_UP),
        }
        assert rows["auto-browser.example.test"]["read_by"] == "auto"
        assert rows["auto-browser.example.test"]["read_found"] == "browser"
        assert effective_read(untested) is None
        # a switched-off site says nothing, whatever it is read by
        await where.switch(session, OWNER, sites["auto-browser"].id, False)
        [row] = await where_view.listing(session, OWNER, sites["auto-browser"].id)
        assert (row["state"], row["status"]) == ("off", "")


async def test_only_a_site_the_app_reads_gets_a_project_one_found_for_the_browser_pauses_and_one_found_open_resumes(js):
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await load_profile(session, OWNER)
        open_ = await where.add(session, OWNER, "https://open.example.test/jobs", "job_board", read_by=AUTO)
        walled = await where.add(session, OWNER, "https://walled.example.test/jobs", "job_board", read_by=AUTO)
        untested = await where.add(session, OWNER, "https://untested.example.test/jobs", "job_board", read_by=AUTO)
        await where.set_found(session, open_, BY_APP, OPEN)
        await where.set_found(session, walled, BY_BROWSER, read_probe.SIGN_IN_NOTE)
        made = await autostart.ensure_projects(session, OWNER)
        assert [p.site_id for p in made] == [open_.id], "none for a site found to need the browser, or not tested"
        with pytest.raises(PlanEngineError, match="browser"):
            await projects.create_project(session, OWNER, walled.id, "Read it", "Read it", created_by="you")
        with pytest.raises(PlanEngineError, match="not tested"):
            await projects.create_project(session, OWNER, untested.id, "Read it", "Read it", created_by="you")
        # found to need the browser later: the project being learned is paused; found open again, picked up
        project = made[0]
        await where.set_found(session, open_, BY_BROWSER, read_probe.TURNED_AWAY_NOTE)
        assert project.stopped_at is not None and project.stop_reason == where.BROWSER_PAUSE
        await where.set_found(session, open_, BY_APP, OPEN)
        assert project.stopped_at is None and project.stop_reason is None
        # only a site left to resumebot takes a finding, and only one of the two
        chosen = await where.add(session, OWNER, "https://chosen.example.test/jobs", "job_board", read_by=BY_APP)
        await where.set_found(session, chosen, BY_BROWSER, "ignored")
        assert (chosen.read_found, chosen.read_note) == (None, None)
        for bad in ("carrier pigeon", None):
            with pytest.raises(where.WhereError):
                await where.set_found(session, open_, bad, "x")


# --- the night's read ----------------------------------------------------------------------------------------------


@dataclass
class _Listing:
    postings: list[dict]
    truncated: bool = False
    note: str | None = None


@dataclass
class _Board:
    asked: list = field(default_factory=list)

    async def read(self, fetch, board):
        self.asked.append(board["slug"])
        return _Listing([{"ext_id": "1", "title": f"Role at {board['slug']}", "location": "Remote",
                          "work_mode": "remote", "comp": None, "comp_min": None, "comp_max": None, "posted": None,
                          "url": f"https://jobs.example.test/{board['slug']}/1", "apply_url": None,
                          "text": "About it.", "department": None}])

    async def detail(self, fetch, board, posting):
        return {}


async def test_the_nights_read_reads_sites_found_open_and_leaves_the_browsers_and_the_untested_alone(js, monkeypatch):
    board = _Board()
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: board})
    monkeypatch.setattr(polite, "make", open_internet())
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        for name in ("Alpha", "Bravo", "Charlie", "Delta"):
            employer = await employers.find_or_make(session, OWNER, name)
            employer.careers_url, employer.check_directly = f"https://{name.lower()}.example.test/jobs", True
            employer.ats, employer.board = EXAMPLE_SYSTEM, {"slug": name.lower()}
        await session.flush()
        await where.seed_employer_sites(session, OWNER)
        by_employer = {e.name: (await session.scalars(select(Site).where(Site.employer_id == e.id))).one()
                       for e in (await session.scalars(select(Employer))).all()}
        for name, found in (("Alpha", BY_APP), ("Bravo", BY_BROWSER), ("Charlie", None)):
            by_employer[name].read_by, by_employer[name].read_found = AUTO, found
        await session.flush()
        assert [e.name for e in await where.watched_employers(session, OWNER)] == ["Alpha", "Delta"]
        read = await actions.read_boards(session, None, {"owner": OWNER})
        assert read["employers"] == 2 and sorted(board.asked) == ["alpha", "delta"]
        assert {p.company for p in (await session.scalars(select(JobPosting))).all()} == {"Alpha", "Delta"}
        rows = {r["label"]: r["state"] for r in await where_view.listing(session, OWNER)}
        assert (rows["Alpha"], rows["Bravo"], rows["Delta"]) == ("working", "browser", "working")
        # an employer whose site works through its job system stays under that system while it is found open
        through = await where_view.through_systems(session, OWNER, await where_view.listing(session, OWNER))
        assert {str(by_employer[n].id): through.get(str(by_employer[n].id)) for n in ("Alpha", "Bravo", "Delta")} == {
            str(by_employer["Alpha"].id): EXAMPLE_SYSTEM, str(by_employer["Bravo"].id): None,
            str(by_employer["Delta"].id): EXAMPLE_SYSTEM}
        # found to need the browser, the next night's read leaves it alone
        await where.set_found(session, by_employer["Alpha"], BY_BROWSER, "It asks for a sign-in.")
        board.asked.clear()
        assert (await actions.read_boards(session, None, {"owner": OWNER}))["employers"] == 1
        assert board.asked == ["delta"]
