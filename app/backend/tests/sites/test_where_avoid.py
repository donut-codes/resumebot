"""Where to look's two lists and the browser marker (docs/PRD.md 30.3): sites to search (the owner's own, and every
employer's site that is switched on) and sites to avoid (matched by host, never read, a posting on one stopped by the
filter), a site never on both lists, and a site read in the owner's own browser left alone by the night's read and by
the site projects. Every host is made up (example.test); no network."""

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
from app.jobsearch.gating import sift
from app.jobsearch.models import JobPosting
from app.jobsearch.profile import store as profile_store
from app.sites import autostart, avoid, completion, projects, where, where_view
from app.sites.base import PlanEngineError
from app.sites.models import Site
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

SITES = "/api/v1/sites"


async def _employer(session, name, careers, *, watched=False, system=None, board=None):
    employer = await employers.find_or_make(session, OWNER, name)
    employer.careers_url, employer.check_directly = careers, watched
    employer.ats, employer.board = system, board or {}
    await session.flush()
    return employer


async def _site(session, url) -> Site:
    return await session.scalar(select(Site).where(Site.owner == OWNER, Site.url == url))


async def _count(session) -> int:
    return len((await session.scalars(select(Site).where(Site.owner == OWNER))).all())


# --- matching a link to a site to avoid ----------------------------------------------------------------------------


def test_a_link_is_on_an_avoided_host_by_host_with_www_dropped_and_subdomains_counted():
    avoided = frozenset({"jobs.example.test"})

    def on(*urls):
        return avoid.on_list(urls, avoided)

    assert on("https://jobs.example.test/viewjob?id=1")
    assert on("https://WWW.Jobs.Example.test/"), "case and www do not matter"
    assert on("https://uk.jobs.example.test/job/1"), "a subdomain of an avoided host counts"
    assert on("https://jobs.example.test:8443/x")
    assert on("jobs.example.test/x"), "an address with no scheme is read the same way"
    assert on(None, "https://elsewhere.example.test/", "https://jobs.example.test/apply"), "either link"
    assert not on("https://example.test/jobs.example.test"), "only the host counts, not the path"
    assert not on("https://notjobs.example.test/"), "a longer name is another site"
    assert not on("https://jobs.example.test.other.test/")
    assert not on("https://example.test/"), "the parent of an avoided host is not avoided"
    assert not on(None, "", "   ")
    assert not avoid.on_list(["https://jobs.example.test/"], frozenset())
    assert avoid.host_of("https://www.Jobs.Example.test./a") == "jobs.example.test"
    assert avoid.host_of("") is None and avoid.host_of(None) is None and avoid.host_of("https:///x") is None
    assert avoid.related("a.example.test", "example.test") and avoid.related("example.test", "a.example.test")
    assert not avoid.related("a.example.test", "b.example.test")
    # the browser reader reads a host the same way (there were two readers, and they disagreed on a trailing dot)
    from app.jobsearch.browser import reader as browser_reader

    assert browser_reader.host_of("https://www.Jobs.Example.test./a") == "jobs.example.test"
    assert browser_reader.host_of("") == "" and browser_reader.host_of(None) == ""


# --- the list to avoid ----------------------------------------------------------------------------------------------


async def test_a_site_to_avoid_is_kept_as_its_host_and_is_never_on_or_read(js):
    async with db.session_factory()() as session:
        site = await where.add(session, OWNER, "https://www.Jobs.Example.test/viewjob?id=1", list_name="avoid")
        assert (site.url, site.list_name, site.on, site.read_by, site.added_by, site.turned_on_at) == (
            "jobs.example.test", "avoid", False, "app", "owner", None)
        [row] = await where_view.listing(session, OWNER, site.id)
        assert (row["list"], row["on"], row["state"], row["label"], row["skill"], row["roles"]) == (
            "avoid", False, "off", "jobs.example.test", None, 0)
        # any address on the same host is the one entry; an address with no scheme is taken as https
        for again in ("jobs.example.test", "https://www.jobs.example.test/other", "HTTP://JOBS.EXAMPLE.TEST"):
            assert (await where.add(session, OWNER, again, list_name="avoid")).id == site.id
        assert await _count(session) == 1
        assert await avoid.hosts(session, OWNER) == frozenset({"jobs.example.test"})
        assert await avoid.hosts(session, "someone else") == frozenset()
        with pytest.raises(where.WhereError, match="no switch"):
            await where.switch(session, OWNER, site.id, True)
        with pytest.raises(where.WhereError, match="never read"):
            await where.set_read_by(session, OWNER, site.id, "browser")
        for bad in ("http://localhost/jobs", "jobs", "", "https://user:pw@jobs.example.test/", "http://10.0.0.5/"):
            with pytest.raises(where.WhereError):
                await where.add(session, OWNER, bad, list_name="avoid")
        with pytest.raises(where.WhereError, match="search, avoid"):
            await where.add(session, OWNER, "https://x.example.test/", list_name="ignore")
        # it has no project, however it is asked for
        with pytest.raises(PlanEngineError, match="avoid"):
            await projects.create_project(session, OWNER, site.id, "Read it", "Read it", created_by="you")
        assert await autostart.ensure_projects(session, OWNER) == []
        await where.remove(session, OWNER, site.id)
        assert await _count(session) == 0
        with pytest.raises(where.NoSuchSite):
            await where.remove(session, OWNER, site.id)


async def test_an_employer_whose_address_is_on_an_avoided_host_gets_no_site(js):
    async with db.session_factory()() as session:
        await _employer(session, "Quux", "https://quux.jobs.example.test/careers")
        await _employer(session, "Corge", "https://corge.example.test/careers")
        await where.add(session, OWNER, "jobs.example.test", list_name="avoid")
        assert await where.seed_employer_sites(session, OWNER) == {"sites": 1, "skills": 0}
        assert await _site(session, "https://quux.jobs.example.test/careers") is None
        assert (await _site(session, "https://corge.example.test/careers")).list_name == "search"


# --- a site is never on both lists ---------------------------------------------------------------------------------


async def test_a_site_is_never_on_both_lists_and_the_refusal_says_so(client):
    made = await client.post(SITES, json={"url": "https://jobs.example.test/board", "kind": "job_board"})
    assert made.status_code == 200 and made.json()["list"] == "search"
    site_id = made.json()["id"]
    # avoiding what is searched: the same host, a subdomain of it and its parent are all refused, with a 409
    for url in ("https://www.jobs.example.test/other", "https://uk.jobs.example.test/", "example.test"):
        refused = await client.post(SITES, json={"url": url, "list": "avoid"})
        assert refused.status_code == 409, url
        assert refused.json()["detail"] == (
            "jobs.example.test is on your list of sites to search. Switch it off there first.")
    assert (await client.post(SITES, json={"url": "https://other.example.test", "list": "avoid"})).status_code == 200
    # switched off, it is no longer searched, so it can be avoided; then it cannot be switched back on
    assert (await client.patch(f"{SITES}/{site_id}", json={"on": False})).status_code == 200
    avoided = await client.post(SITES, json={"url": "https://jobs.example.test/", "list": "avoid"})
    assert avoided.status_code == 200 and (avoided.json()["list"], avoided.json()["on"]) == ("avoid", False)
    on = await client.patch(f"{SITES}/{site_id}", json={"on": True})
    assert on.status_code == 409 and on.json()["detail"] == (
        "jobs.example.test is on your list of sites to avoid. Take it off that list first.")
    # and what is avoided cannot be added to search, by its own address or any other on its host
    for url in ("https://jobs.example.test/board", "https://jobs.example.test/new", "https://x.jobs.example.test"):
        again = await client.post(SITES, json={"url": url, "kind": "job_board"})
        assert again.status_code == 409 and "list of sites to avoid" in again.json()["detail"], url
    # taken off the list to avoid, it can be searched again
    taken_off = await client.delete(f"{SITES}/{avoided.json()['id']}")
    assert taken_off.json() == {"ok": True, "said": "Re-checked 0 postings and 0 roles: nothing changed."}
    back = await client.patch(f"{SITES}/{site_id}", json={"on": True})
    assert back.status_code == 200 and back.json()["on"] is True


async def test_the_same_address_added_twice_to_one_list_gives_the_one_site(client):
    first = await client.post(SITES, json={"url": "https://board.example.test/jobs"})
    again = await client.post(SITES, json={"url": "https://board.example.test/jobs", "kind": "association"})
    assert first.json()["id"] == again.json()["id"] and again.json()["kind"] == "job_board"
    one = await client.post(SITES, json={"url": "https://bad.example.test/x", "list": "avoid"})
    two = await client.post(SITES, json={"url": "https://www.bad.example.test/y", "list": "avoid"})
    assert one.json()["id"] == two.json()["id"]
    rows = (await client.get(SITES)).json()["sites"]
    assert sorted((r["label"], r["list"]) for r in rows) == [("bad.example.test", "avoid"),
                                                              ("board.example.test", "search")]


async def test_a_watched_employer_site_blocks_avoiding_its_host_until_it_is_switched_off(client):
    async with db.session_factory()() as session:
        await _employer(session, "Borealis", "https://borealis.example.test/jobs", watched=True)
        await _employer(session, "Acme", "https://acme.example.test/jobs")
        await where.seed_employer_sites(session, OWNER)
        await session.commit()
    refused = await client.post(SITES, json={"url": "borealis.example.test", "list": "avoid"})
    assert refused.status_code == 409 and "borealis.example.test is on your list of sites to search" in (
        refused.json()["detail"])
    # an employer's site that is off is not on the list to search, so its host can be avoided; it cannot be watched then
    assert (await client.post(SITES, json={"url": "acme.example.test", "list": "avoid"})).status_code == 200
    async with db.session_factory()() as session:
        acme = await _site(session, "https://acme.example.test/jobs")
        acme_id = acme.id
    watch = await client.patch(f"{SITES}/{acme_id}", json={"on": True})
    assert watch.status_code == 409 and "list of sites to avoid" in watch.json()["detail"]
    async with db.session_factory()() as session:
        acme = await session.get(Site, acme_id)
        assert acme.on is False
        employer = await employers.find(session, OWNER, "Acme")
        assert employer.check_directly is False, "and the employer is not marked watched"
    # off from here, the watched one can then be avoided
    async with db.session_factory()() as session:
        borealis_id = (await _site(session, "https://borealis.example.test/jobs")).id
    assert (await client.patch(f"{SITES}/{borealis_id}", json={"on": False})).status_code == 200
    assert (await client.post(SITES, json={"url": "borealis.example.test", "list": "avoid"})).status_code == 200


# --- what the page lists --------------------------------------------------------------------------------------------


async def test_the_page_lists_watched_employer_sites_with_the_owners_own_and_switches_one_off_only(client):
    async with db.session_factory()() as session:
        await _employer(session, "Borealis", "https://borealis.example.test/jobs", watched=True)
        await _employer(session, "Acme", "https://acme.example.test/jobs")
        await where.seed_employer_sites(session, OWNER)
        await session.commit()
    assert (await client.post(SITES, json={"url": "https://board.example.test/jobs"})).status_code == 200
    assert (await client.post(SITES, json={"url": "https://bad.example.test", "list": "avoid"})).status_code == 200
    rows = (await client.get(SITES)).json()["sites"]
    assert sorted((r["label"], r["list"], r["on"]) for r in rows) == [
        ("Borealis", "search", True), ("bad.example.test", "avoid", False), ("board.example.test", "search", True)], (
        "Acme is an employer's site that is off: it is not listed here")
    borealis = next(r for r in rows if r["label"] == "Borealis")
    assert borealis["employer_id"] and borealis["kind"] == "employer"
    # switched off here, it also clears the employer's watched mark, and it leaves the page
    off = await client.patch(f"{SITES}/{borealis['id']}", json={"on": False})
    assert off.status_code == 200 and off.json()["on"] is False and off.json()["state"] == "off"
    async with db.session_factory()() as session:
        assert (await employers.find(session, OWNER, "Borealis")).check_directly is False
        assert [e.name for e in await where.watched_employers(session, OWNER)] == []
        # every site is still known to the Employers screen, which switches it on again
        assert {s["label"] for s in await where_view.listing(session, OWNER)} >= {"Borealis", "Acme"}
    assert "Borealis" not in [r["label"] for r in (await client.get(SITES)).json()["sites"]]
    on = await client.patch(f"{SITES}/{borealis['id']}", json={"on": True})
    assert on.status_code == 200
    assert "Borealis" in [r["label"] for r in (await client.get(SITES)).json()["sites"]]


# --- the filter -----------------------------------------------------------------------------------------------------


NOW = datetime(2026, 10, 9, 5, 30, tzinfo=UTC)


def _posting(employer, ext_id, title, **more):
    return JobPosting(owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=ext_id, company=employer.name,
                      title=title, first_seen=NOW, last_seen=NOW, links=[], location="Remote", work_mode="remote",
                      **more)


async def test_the_filter_stops_a_posting_whose_link_goes_to_an_avoided_site_and_says_why(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={**PROFILE["criteria"], "search_plan": ["data governance"]})
        profile = await profile_store.current(session, OWNER)
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        await where.add(session, OWNER, "https://www.jobs.example.test/", list_name="avoid")
        session.add_all([
            _posting(fab, "1", "Data Governance Lead", url="https://jobs.example.test/job/1"),
            _posting(fab, "2", "Data Governance Lead", url="https://fabrikam.example.test/careers/2",
                     apply_url="https://uk.jobs.example.test/apply/2"),
            _posting(fab, "3", "Data Governance Lead", url="https://fabrikam.example.test/careers/3",
                     apply_url="https://fabrikam.example.test/apply/3"),
            _posting(fab, "4", "Data Governance Lead", url="https://notjobs.example.test/job/4"),
            _posting(fab, "5", "Data Governance Lead"),
        ])
        await session.flush()
        result = await sift.sift(session, OWNER, profile, now=NOW)
        assert result["stopped"] == {"a site you avoid": 2}
        assert sorted(c["posting"].ext_id for c in result["candidates"]) == ["3", "4", "5"]
        by_id = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
        assert by_id["1"].filtered_by == by_id["2"].filtered_by == "a site you avoid" == avoid.REASON
        assert by_id["1"].route is None and by_id["3"].filtered_by is None and by_id["3"].route == "roles"


# --- read in your browser -------------------------------------------------------------------------------------------


async def test_a_site_can_be_added_and_changed_to_be_read_in_the_browser(client):
    made = await client.post(SITES, json={"url": "https://board.example.test/jobs", "read_by": "browser"})
    assert made.status_code == 200
    assert (made.json()["on"], made.json()["read_by"], made.json()["state"]) == (True, "browser", "browser")
    other = await client.post(SITES, json={"url": "https://other.example.test/jobs"})
    assert (other.json()["read_by"], other.json()["state"]) == ("auto", "waiting"), "left to resumebot, found open"
    changed = await client.patch(f"{SITES}/{other.json()['id']}", json={"read_by": "browser"})
    assert changed.status_code == 200 and (changed.json()["read_by"], changed.json()["state"]) == ("browser", "browser")
    assert changed.json()["on"] is True, "the switch is separate"
    both = await client.patch(f"{SITES}/{other.json()['id']}", json={"read_by": "app", "on": False})
    assert (both.json()["read_by"], both.json()["on"], both.json()["state"]) == ("app", False, "off")
    # switched off, a browser site says nothing; the marker stays on the row
    off = await client.patch(f"{SITES}/{made.json()['id']}", json={"on": False})
    assert (off.json()["read_by"], off.json()["state"]) == ("browser", "off")
    for body in ({}, {"read_by": "carrier pigeon"}):
        assert (await client.patch(f"{SITES}/{made.json()['id']}", json=body)).status_code == 422
    bad = await client.post(SITES, json={"url": "https://third.example.test/", "read_by": "elsewhere"})
    assert bad.status_code == 422
    missing = await client.patch(f"{SITES}/00000000-0000-0000-0000-000000000000", json={"read_by": "app"})
    assert missing.status_code == 404
    async with db.session_factory()() as session:
        assert [s.url for s in await where.browser_sites(session, OWNER)] == [], "off: the reader has none"
        await where.switch(session, OWNER, (await _site(session, "https://board.example.test/jobs")).id, True)
        assert [s.url for s in await where.browser_sites(session, OWNER)] == ["https://board.example.test/jobs"]


async def test_a_site_read_in_the_browser_gets_no_project_and_one_already_open_waits(js):
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await load_profile(session, OWNER)
        phone = await where.add(session, OWNER, "https://phone.example.test/jobs", read_by="browser")
        plain = await where.add(session, OWNER, "https://plain.example.test/jobs")
        with pytest.raises(PlanEngineError, match="browser"):
            await projects.create_project(session, OWNER, phone.id, "Read it", "Read it", created_by="you")
        made = await autostart.ensure_projects(session, OWNER)
        assert [p.site_id for p in made] == [plain.id], "only the site the app reads gets a project"
        # a project that is open when the site is set to the browser is paused, and picked up again when set back
        [row] = await where_view.listing(session, OWNER, plain.id)
        assert row["state"] == "learning"
        await where.set_read_by(session, OWNER, plain.id, "browser")
        project = made[0]
        assert project.stopped_at is not None and project.stop_reason == where.BROWSER_PAUSE
        [row] = await where_view.listing(session, OWNER, plain.id)
        assert (row["state"], row["needs"]) == ("browser", None)
        await where.set_read_by(session, OWNER, plain.id, "app")
        assert project.stopped_at is None and project.stop_reason is None
        [row] = await where_view.listing(session, OWNER, plain.id)
        assert row["state"] == "learning"
        # a project the owner paused themselves stays paused
        await completion.stop_project(session, project, "later")
        await where.set_read_by(session, OWNER, plain.id, "browser")
        await where.set_read_by(session, OWNER, plain.id, "app")
        assert project.stopped_at is not None and project.stop_reason == "later"


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


def _mock_fetch():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(200, text="<html/>")

    return lambda: Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)


async def test_the_nights_read_leaves_a_watched_employer_alone_when_its_site_is_read_in_the_browser(js, monkeypatch):
    board = _Board()
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: board})
    monkeypatch.setattr(polite, "make", _mock_fetch())
    async with db.session_factory()() as session:
        for name in ("Borealis", "Cirrus"):
            await _employer(session, name, f"https://{name.lower()}.example.test/jobs", watched=True,
                            system=EXAMPLE_SYSTEM, board={"slug": name.lower()})
        await where.seed_employer_sites(session, OWNER)
        cirrus = await _site(session, "https://cirrus.example.test/jobs")
        await where.set_read_by(session, OWNER, cirrus.id, "browser")
        assert [e.name for e in await where.watched_employers(session, OWNER)] == ["Borealis"]
        read = await actions.read_boards(session, None, {"owner": OWNER})
        assert read["employers"] == 1 and board.asked == ["borealis"], "the browser site is never fetched"
        assert {p.company for p in (await session.scalars(select(JobPosting))).all()} == {"Borealis"}
        # the employer is still watched, and its site says where it is read
        [row] = await where_view.listing(session, OWNER, cirrus.id)
        assert (row["on"], row["state"], row["read_by"]) == (True, "browser", "browser")
        await where.set_read_by(session, OWNER, cirrus.id, "app")
        board.asked.clear()
        assert (await actions.read_boards(session, None, {"owner": OWNER}))["employers"] == 2
        assert sorted(board.asked) == ["borealis", "cirrus"]
