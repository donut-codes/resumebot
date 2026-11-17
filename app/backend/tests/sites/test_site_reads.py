"""The night's read of the sites the scout learned (docs/PRD.md 29.2, 29.4 and 30.1 step 2): a switched-on site whose
skill is a saved recipe (a data request replayed, list pages read) is read by the night and its postings go into the
posting index like a board's; a recipe that breaks is retired and the site's project reopens at the next mission; a
site with a built-in adapter is read once, and an off site, a site read in the owner's browser, an avoided one and one
whose skill cannot be run yet are not read. Every site is made up and sits behind a mock transport; no network."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import actions, adapters
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import boards_night
from app.jobsearch.models import Employer, JobPosting
from app.jobsearch.postings import site_reads
from app.sites import autostart, skills, where, where_view
from app.sites.models import PlanTask, Site, SiteSkill
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

BOARD = "jobs.example.test"
CAREERS = "careers.initech.example.test"


@dataclass
class Web:
    """The made-up internet: each host answers through a handler the test sets; every request that is not for a
    robots.txt is noted as `host/path`."""

    hosts: dict[str, Callable[[httpx.Request], httpx.Response]] = field(default_factory=dict)
    seen: list[str] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        self.seen.append(f"{request.url.host}{request.url.path}")
        handler = self.hosts.get(request.url.host)
        return handler(request) if handler else httpx.Response(404)

    def fetcher(self) -> Fetcher:
        return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(self.handle)), per_host_seconds=0)

    def careers(self, host: str, *jobs: tuple[int, str]) -> None:
        """The host's /careers page lists these jobs, each a link to /careers/jobs/<n>-x."""
        items = "".join(f'<li><a href="/careers/jobs/{n}-x">{title}</a></li>' for n, title in jobs)
        page = f'<html><body><ul>{items}</ul><a href="/about">About us</a></body></html>'
        self.hosts[host] = lambda r: httpx.Response(200, text=page) if r.url.path == "/careers" else httpx.Response(404)

    def api(self, host: str, answer: Callable[[], httpx.Response]) -> None:
        """The host's /api/jobs answers as `answer` says."""
        self.hosts[host] = lambda r: answer() if r.url.path == "/api/jobs" else httpx.Response(404)

    def rows(self, host: str, *rows: dict) -> None:
        self.api(host, lambda: httpx.Response(200, json={"data": list(rows)}))


@pytest.fixture
def web(monkeypatch):
    internet = Web()
    monkeypatch.setattr(polite, "make", internet.fetcher)  # every step reads through the mock: no network
    return internet


def _list_page(host: str = CAREERS) -> dict:
    return {"kind": "list_page", "url": f"https://{host}/careers", "link_pattern": r"/careers/jobs/\d+"}


def _request(host: str = BOARD, **fields: str) -> dict:
    return {"kind": "request", "method": "GET", "url": f"https://{host}/api/jobs", "items": "data",
            "fields": {"title": "name", "url": "path", "ext_id": "id", "location": "loc", **fields},
            "url_prefix": f"https://{host}"}


def _row(n: int, **more) -> dict:
    return {"id": n, "name": f"Role {n}", "path": f"/jobs/{n}", "loc": "Remote", **more}


async def _employer_site(session, name: str, host: str, recipe: dict | None = None, **over):
    """An employer the owner watches, with its site on and (when given) the recipe a project learned as its skill."""
    employer = await employers.find_or_make(session, OWNER, name)
    employer.careers_url, employer.check_directly = f"https://{host}/careers", True
    for key, value in over.items():
        setattr(employer, key, value)
    await session.flush()
    await where.seed_employer_sites(session, OWNER)
    site = (await session.scalars(select(Site).where(Site.employer_id == employer.id))).one()
    if recipe is not None:
        await skills.save_skill(session, OWNER, site.id, recipe)
    return employer, site


async def _board_site(session, recipe: dict | None, host: str = BOARD, **kw) -> Site:
    """A job board the owner added (it belongs to no employer), with the recipe a project learned as its skill."""
    site = await where.add(session, OWNER, f"https://{host}/remote", "job_board", **kw)
    if recipe is not None:
        await skills.save_skill(session, OWNER, site.id, recipe)
    return site


async def _read(session) -> dict:
    return await actions.read_boards(session, None, {"owner": OWNER})


async def _postings(session) -> dict[str, JobPosting]:
    return {p.title: p for p in (await session.scalars(select(JobPosting))).all()}


async def _state(session, site) -> str:
    [row] = await where_view.listing(session, OWNER, site.id)
    return row["state"]


# --- what is read, and where the postings go --------------------------------------------------------------------------


async def test_an_employers_list_page_skill_is_read_into_the_index_under_the_employer_and_kept_up_to_date(js, web):
    web.careers(CAREERS, (1, "Data Analyst"), (2, "Team Lead"))
    async with db.session_factory()() as session:
        initech, _site = await _employer_site(session, "Initech", CAREERS, _list_page())
        read = await _read(session)
        assert (read["employers"], read["failed"], read["seen"], read["new"], read["closed"]) == (1, 0, 2, 2, 0)
        assert read["faults"] == [] and web.seen == [f"{CAREERS}/careers"], "one polite request for the page"
        rows = await _postings(session)
        analyst, lead = rows["Data Analyst"], rows["Team Lead"]
        assert (analyst.employer_id, analyst.company, analyst.ats) == (initech.id, "Initech", "own_site")
        assert analyst.url == f"https://{CAREERS}/careers/jobs/1-x" and analyst.links == [analyst.url]
        assert analyst.match_key == match_key("Initech", "Data Analyst")
        assert analyst.first_seen == analyst.last_seen and analyst.closed_at is None
        assert (initech.fetch_ok, initech.open_roles, initech.fetch_note) == (True, 2, None)
        # the next night: one is gone, the other is still there, and first seen is never moved
        first = analyst.first_seen
        web.careers(CAREERS, (1, "Data Analyst"), (3, "Ops Manager"))
        again = await _read(session)
        assert (again["seen"], again["new"], again["closed"]) == (2, 1, 1)
        assert lead.closed_at is not None and analyst.closed_at is None
        assert analyst.first_seen == first and analyst.last_seen > first
        assert (await _postings(session))["Ops Manager"].employer_id == initech.id
        assert initech.open_roles == 2


async def test_a_job_boards_request_skill_is_read_under_the_site_with_the_posting_s_own_company(js, web):
    web.rows(BOARD, _row(11, org="Umbrella Co"), _row(12, org=None), _row(13, org="Umbrella Co"))
    async with db.session_factory()() as session:
        site = await _board_site(session, _request(company="org"))
        read = await _read(session)
        assert (read["employers"], read["seen"], read["new"]) == (1, 3, 3)
        rows = await _postings(session)
        grants = rows["Role 11"]
        assert (grants.employer_id, grants.company, grants.ats) == (site.id, "Umbrella Co", "job_site")
        assert grants.ext_id == "11" and grants.url == f"https://{BOARD}/jobs/11"
        assert grants.match_key == match_key("Umbrella Co", "Role 11"), "the same job elsewhere is the same posting"
        assert rows["Role 12"].company == "Example", "no company named: the host's name, not the bare host"
        assert (await session.scalars(select(Employer))).all() == [], "no employer record for a name on a board"
        # the next night adds one and drops one; the ones still listed are not new
        web.rows(BOARD, _row(11, org="Umbrella Co"), _row(14, org="Umbrella Co"))
        again = await _read(session)
        assert (again["seen"], again["new"], again["closed"]) == (2, 1, 2)
        assert rows["Role 12"].closed_at is not None and grants.closed_at is None


async def test_on_an_employers_own_site_the_postings_are_the_employers_whatever_a_company_field_says(js, web):
    hooli_host = "api.hooli.example.test"
    web.rows(hooli_host, _row(1, org="Somebody Else"), _row(2, org="Somebody Else"))
    web.rows(BOARD, _row(5, org="Umbrella Co"))
    async with db.session_factory()() as session:
        hooli, _ = await _employer_site(session, "Hooli", hooli_host, _request(hooli_host, company="org"))
        await _board_site(session, _request(company="org"))
        await _read(session)
        rows = await _postings(session)
        assert {(rows[t].company, rows[t].employer_id == hooli.id) for t in ("Role 1", "Role 2")} == {("Hooli", True)}
        assert rows["Role 5"].company == "Umbrella Co", "and on a job board it is the posting's own"


# --- who reads what ---------------------------------------------------------------------------------------------------


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
        return {"text": f"The whole posting for {posting['title']}."}


async def test_a_site_with_a_built_in_adapter_is_read_once_and_one_with_a_learned_skill_by_its_skill_alone(
        js, web, monkeypatch):
    board = Board()
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: board})
    web.careers("careers.hooli.example.test", (1, "Hooli Role"))
    async with db.session_factory()() as session:
        globex, globex_site = await _employer_site(
            session, "Globex", "careers.globex.example.test", None, ats=EXAMPLE_SYSTEM, board={"slug": "globex"})
        # Hooli is on a known system too, but its reader broke and a project learned a page reader in its place
        hooli, hooli_site = await _employer_site(
            session, "Hooli", "careers.hooli.example.test", None, ats=EXAMPLE_SYSTEM, board={"slug": "hooli"})
        await skills.retire_skill(session, await skills.current_skill(session, hooli_site.id))
        await skills.save_skill(session, OWNER, hooli_site.id, _list_page("careers.hooli.example.test"))
        assert (await skills.current_skill(session, globex_site.id)).recipe["kind"] == "adapter"
        read = await _read(session)
        assert board.asked == ["globex"], "Hooli's adapter is not asked: its skill is the page reader"
        assert web.seen == ["careers.hooli.example.test/careers"], "and Globex's site is not fetched a second time"
        assert (read["employers"], read["failed"], read["seen"], read["new"]) == (2, 0, 2, 2)
        rows = await _postings(session)
        assert (rows["Role at globex"].ats, rows["Role at globex"].employer_id) == (EXAMPLE_SYSTEM, globex.id)
        assert (rows["Hooli Role"].ats, rows["Hooli Role"].employer_id) == ("own_site", hooli.id)
        # a second night reads each of them once more, and neither doubles
        board.asked.clear()
        web.seen.clear()
        again = await _read(session)
        assert board.asked == ["globex"] and web.seen == ["careers.hooli.example.test/careers"]
        assert (again["new"], again["seen"]) == (0, 2) and len(await _postings(session)) == 2


async def test_an_adapter_skill_on_a_job_board_is_run_by_its_skill_but_on_an_employers_site_is_left_to_its_system(
        js, web, monkeypatch):
    """A job system the app can read, learned for a job board (which has no employer): nothing else would read it. An
    employer's own adapter skill is the built-in read's: it is not asked a second time through the recipe."""
    board = Board()
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: board})
    monkeypatch.setattr(skills, "ADAPTERS", {EXAMPLE_SYSTEM: board})
    async with db.session_factory()() as session:
        site = await _board_site(session, {"kind": "adapter", "ats": EXAMPLE_SYSTEM, "board": {"slug": "assoc"}})
        # an employer whose board went away: `resolve.forget` let it go, though its site still holds the reader
        gone, gone_site = await _employer_site(
            session, "Initrode", "careers.initrode.example.test", None, ats=EXAMPLE_SYSTEM, board={"slug": "initrode"})
        assert (await skills.current_skill(session, gone_site.id)).recipe["kind"] == "adapter"
        gone.ats, gone.board = None, {"gone": [{"ats": EXAMPLE_SYSTEM, "board": {"slug": "initrode"}}]}
        read = await _read(session)
        assert board.asked == ["assoc"] and (read["employers"], read["new"]) == (1, 1)
        [posting] = (await session.scalars(select(JobPosting))).all()
        assert (posting.employer_id, posting.ats, posting.company) == (site.id, "job_site", "Example")


async def test_an_off_site_a_browser_site_a_gated_employer_and_a_site_with_no_skill_are_not_read(js, web):
    for host in ("off.example.test", "browser.example.test", "gated.example.test", "bare.example.test", BOARD):
        web.rows(host, _row(1))
    async with db.session_factory()() as session:
        await _employer_site(session, "Offco", "off.example.test", _request("off.example.test"))
        off = (await session.scalars(select(Site).where(Site.url == "https://off.example.test/careers"))).one()
        await where.switch(session, OWNER, off.id, False)
        await _board_site(session, _request("browser.example.test"), "browser.example.test", read_by="browser")
        gated, _ = await _employer_site(session, "Gatedco", "gated.example.test", _request("gated.example.test"))
        gated.gated = True  # the owner gates the employer after its site was made
        await session.flush()
        await _board_site(session, None, "bare.example.test")
        await _board_site(session, _request())  # the one that is read, so the others' silence means something
        read = await _read(session)
        assert web.seen == [f"{BOARD}/api/jobs"], "nothing else is fetched"
        assert (read["employers"], read["failed"], read["faults"]) == (1, 0, [])
        assert [p.title for p in (await session.scalars(select(JobPosting))).all()] == ["Role 1"]


async def test_a_site_whose_skill_is_a_browser_recipe_is_left_alone_and_its_skill_is_not_retired(js, web):
    """The recipe is words for the owner's browser session (docs/PRD.md 37.1): the night does not fetch the site, count
    it as failed or retire the skill, whether it is a job board or an employer's own site."""
    recipe = {
        "kind": "browser", "start_url": "https://words.example.test/jobs",
        "search": {"how": "none"},
        "results": {"row": "a card with a title", "fields": {"title": "the bold line"}, "next": "none"},
        "posting": {"description": "the text under About the role"},
    }
    for host in ("words.example.test", "mine.example.test", BOARD):
        web.rows(host, _row(1))
    async with db.session_factory()() as session:
        board = await _board_site(session, recipe, "words.example.test")
        _, own = await _employer_site(session, "Wordsco", "mine.example.test", recipe)
        await _board_site(session, _request())  # the one that is read, so the others' silence means something
        read = await _read(session)
        assert web.seen == [f"{BOARD}/api/jobs"], "nothing else is fetched"
        assert (read["employers"], read["failed"], read["faults"]) == (1, 0, [])
        for site in (board, own):
            assert (await skills.current_skill(session, site.id)).recipe["kind"] == "browser", "it is not retired"
        assert [p.title for p in (await session.scalars(select(JobPosting))).all()] == ["Role 1"]


async def test_a_site_read_in_the_browser_is_not_fetched_until_it_is_set_back_to_the_app(js, web):
    web.rows(BOARD, _row(1))
    async with db.session_factory()() as session:
        site = await _board_site(session, _request(), read_by="browser")
        assert (await _read(session))["employers"] == 0 and web.seen == []
        await where.set_read_by(session, OWNER, site.id, "app")
        assert (await _read(session))["employers"] == 1 and web.seen == [f"{BOARD}/api/jobs"]


# --- a skill that breaks (29.4) ---------------------------------------------------------------------------------------


BREAKS = [
    pytest.param(lambda: httpx.Response(200, json={"data": []}), "empty", True, id="had roles, answers none"),
    pytest.param(lambda: httpx.Response(200, json={"rows": []}), "shape", True, id="no list where the list was"),
    pytest.param(lambda: httpx.Response(200, text="<html>no json</html>"), "shape", True, id="not json"),
    pytest.param(lambda: httpx.Response(403), "refused", True, id="refused"),
    pytest.param(lambda: httpx.Response(404), "gone", False, id="gone"),
    pytest.param(lambda: httpx.Response(503), "network", False, id="unreachable tonight"),
    pytest.param(lambda: httpx.Response(429, headers={"Retry-After": "0"}), "network", False,
                 id="asked to slow down"),
]


@pytest.mark.parametrize(("answer", "kind", "retired"), BREAKS)
async def test_a_job_boards_skill_that_stops_fitting_is_retired_and_its_project_starts_at_the_next_mission(
        js, web, answer, kind, retired):
    web.rows(BOARD, _row(1), _row(2))
    async with db.session_factory()() as session:
        site = await _board_site(session, _request())
        assert (await _read(session))["new"] == 2
        web.api(BOARD, answer)
        read = await _read(session)
        [fault] = read["faults"]
        assert (fault["kind"], fault["site_id"], fault["employer_id"], fault["employer"]) == (
            kind, str(site.id), None, BOARD)
        assert (read["employers"], read["new"]) == (1, 0)
        if retired:
            assert await skills.current_skill(session, site.id) is None
            assert (await skills.broken(session, site.id)).version == 1, "kept, not deleted"
            assert await _state(session, site) == "waiting"
            # tonight's read leaves it alone while its project learns the new shape, and the mission starts the project
            web.seen.clear()
            assert (await _read(session))["employers"] == 0 and web.seen == []
            [project] = await autostart.ensure_projects(session, OWNER, limit=3, by_switch=True)
            assert project.site_id == site.id and await _state(session, site) == "learning"
        else:
            assert (await skills.current_skill(session, site.id)).version == 1
            assert await _state(session, site) == "working"
            assert await autostart.ensure_projects(session, OWNER, limit=3, by_switch=True) == []
            assert all(p.closed_at is None for p in (await _postings(session)).values()), (
                "a read that failed closes nothing")


async def test_an_employers_skill_that_answers_nothing_where_it_had_roles_is_retired_and_says_so_on_the_employer(
        js, web):
    web.careers(CAREERS, (1, "Data Analyst"), (2, "Team Lead"))
    async with db.session_factory()() as session:
        initech, site = await _employer_site(session, "Initech", CAREERS, _list_page())
        assert (await _read(session))["new"] == 2 and initech.open_roles == 2
        web.careers(CAREERS)  # the page now lists no jobs this skill can find
        read = await _read(session)
        [fault] = read["faults"]
        assert (fault["kind"], fault["employer"], fault["employer_id"], fault["site_id"]) == (
            "empty", "Initech", str(initech.id), str(site.id))
        assert (initech.fetch_ok, initech.fetch_note, initech.open_roles) == (
            False, "had 2 open roles, and this read answered none", 2), "one empty read closes nothing"
        assert all(p.closed_at is None for p in (await _postings(session)).values())
        assert await skills.current_skill(session, site.id) is None and await _state(session, site) == "waiting"
        [project] = await autostart.ensure_projects(session, OWNER, limit=3, by_switch=True)
        assert project.site_id == site.id
        task = (await session.scalars(select(PlanTask).where(PlanTask.project_id == project.id))).first()
        said = task.context["context"]
        assert "version 1" in said and "stopped working" in said and "had 2 open roles" in said


async def test_a_second_empty_read_in_a_row_closes_the_roles_and_the_first_does_not(js, web):
    """One empty answer is as likely a site that failed to show its roles as a board with none. The roles stay
    open, with the baseline they had, until a second read finds none too."""
    web.careers(CAREERS, (1, "Data Analyst"), (2, "Team Lead"))
    async with db.session_factory()() as session:
        initech, site = await _employer_site(session, "Initech", CAREERS, _list_page())
        await _read(session)
        web.careers(CAREERS)
        first = await _read(session)
        assert first["closed"] == 0 and initech.open_roles == 2
        assert all(p.closed_at is None for p in (await _postings(session)).values())
        await skills.save_skill(session, OWNER, site.id, _list_page())  # the project relearned: the next night reads it
        second = await _read(session)
        assert second["closed"] == 2 and [f["kind"] for f in second["faults"]] == ["empty"]
        assert initech.open_roles == 0
        assert all(p.closed_at is not None for p in (await _postings(session)).values())


async def test_a_saved_recipe_the_app_can_no_longer_run_is_a_changed_shape_not_a_failed_night(js, web):
    web.rows(BOARD, _row(1))
    async with db.session_factory()() as session:
        site = await _board_site(session, None)
        session.add(SiteSkill(owner=OWNER, site_id=site.id, version=1, recipe={"kind": "request", "url": "x"},
                              created_at=datetime.now(UTC)))
        await session.flush()
        other = await _board_site(session, _request(), "other.example.test")
        web.rows("other.example.test", _row(7))
        read = await _read(session)
        assert (read["employers"], read["failed"], read["new"]) == (2, 1, 1), "the other site is read all the same"
        [fault] = read["faults"]
        assert fault["kind"] == "shape" and fault["site_id"] == str(site.id)
        assert await skills.current_skill(session, site.id) is None
        assert (await skills.current_skill(session, other.id)).version == 1


# --- the night's time (28.17) -----------------------------------------------------------------------------------------


async def test_a_site_not_begun_when_the_nights_time_runs_out_is_left_alone_and_counted(js, web):
    web.careers(CAREERS, (1, "Data Analyst"))
    async with db.session_factory()() as session:
        initech, _site = await _employer_site(session, "Initech", CAREERS, _list_page())
        token = polite.READ_BY.set(time.monotonic() - 1)  # the read's time ran out before any site was begun
        try:
            read = await _read(session)
        finally:
            polite.READ_BY.reset(token)
        assert (read["employers"], read["unread"], read["cut"], read["failed"], read["seen"]) == (1, 1, 0, 0, 0)
        assert web.seen == [] and await _postings(session) == {}
        assert initech.fetched_at is None and initech.fetch_ok is None, "an unread site is as it was"


async def test_a_site_whose_pages_run_out_of_time_keeps_what_it_read_counts_as_cut_and_closes_nothing(
        js, web, monkeypatch):
    made: list[Fetcher] = []
    monkeypatch.setattr(polite, "make", lambda: made.append(web.fetcher()) or made[-1])
    slow = {"out_of_time": False}

    def pages(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("p", "1"))
        if page == 2 and slow["out_of_time"]:
            made[-1].deadline = time.monotonic() - 1  # the time runs out as the second page is answered
        more = f'<a href="/careers?p={page + 1}">Next</a>' if page < 3 else ""
        return httpx.Response(200, text=f'<a href="/careers/jobs/{page}-x">Role {page}</a>{more}')

    web.hosts[CAREERS] = pages
    async with db.session_factory()() as session:
        initech, _site = await _employer_site(session, "Initech", CAREERS, {**_list_page(), "next_pattern": "^next$"})
        whole = await _read(session)
        assert (whole["seen"], whole["new"], whole["cut"], whole["unread"]) == (3, 3, 0, 0)
        slow["out_of_time"] = True
        cut = await _read(session)
        assert (cut["seen"], cut["cut"], cut["unread"], cut["closed"]) == (2, 1, 0, 0)
        rows = await _postings(session)
        assert rows["Role 3"].closed_at is None, "a read cut short can't say it is gone"
        assert initech.fetch_note == "stopped at 2 postings (the time allowed for the read ran out)"
        assert initech.fetch_ok is True


def test_a_skills_own_errors_are_classified_by_the_status_they_carry():
    for words, status, kind in [("jobs.example.test answered 503", 503, "network"),
                                ("jobs.example.test answered 404", 404, "gone"),
                                ("jobs.example.test answered 403", 403, "refused"),
                                ("jobs.example.test did not answer with JSON", None, "shape"),
                                ("no list found at 'data' in the answer: the site has changed shape", None, "shape")]:
        assert site_reads.fault_kind(skills.SkillError(words, status)) == kind, words
    assert site_reads.fault_kind(polite.Refused("robots.txt asks not to read /jobs")) == "refused"
    assert site_reads.fault_kind(httpx.ConnectTimeout("slow")) == "network"
    assert site_reads.fault_kind(ValueError("not a recipe")) == "shape"


# --- the night as a whole ---------------------------------------------------------------------------------------------


async def test_the_nights_read_counts_a_skill_read_site_as_a_board_and_the_filter_takes_its_postings(js, web):
    web.careers(CAREERS, (1, "Director, Data Governance"), (2, "Payroll Specialist"))
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={**PROFILE["criteria"], "search_plan": ["data governance"]})
        await _employer_site(session, "Initech", CAREERS, _list_page())
        await session.commit()
    outcome, summary, counts = await boards_night.read_boards(OWNER, details_per_night=10)
    assert outcome == "done" and len(counts["instances"]) == 1, "the same playbook, with no step of its own"
    assert (counts["employers"], counts["failed"], counts["seen"], counts["new"], counts["candidates"]) == (
        1, 0, 2, 2, 2)
    assert counts["routes"] == {"roles": 2} and counts["details"] == 0
    assert summary == "Read 1 employer board: 2 new postings.\n2 passed the filter."
    async with db.session_factory()() as session:
        rows = await _postings(session)
    assert rows["Director, Data Governance"].filtered_by is None and rows["Payroll Specialist"].route == "roles"


async def test_a_posting_read_through_a_skill_never_takes_a_description_slot_from_a_job_systems(js, monkeypatch):
    """Nothing can fill in a list page's posting (no job system to ask), so one at the front of the queue would use
    up the night's reads for nothing."""
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: Board()})
    now = datetime.now(UTC)

    def waiting(title: str, system: str, **more) -> JobPosting:
        return JobPosting(owner=OWNER, employer_id=None, ats=system, ext_id=title, company="Co", title=title,
                          first_seen=now, last_seen=now, filtered_at=now, **more)

    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, "Coastal Works")
        employer.ats, employer.board = EXAMPLE_SYSTEM, {"slug": "coastal"}
        learned = waiting("Learned Role", "own_site", route="reader")
        known = waiting("Known Role", EXAMPLE_SYSTEM)
        learned.employer_id = known.employer_id = employer.id
        session.add_all([learned, known])
        await session.flush()
        done = await actions.read_descriptions(session, None, {"owner": OWNER, "details_per_night": 1})
        assert done["filled"] == 1 and known.text and not learned.text


async def test_a_recipes_read_keeps_each_row_naming_its_version_and_a_rebuild_parses_it_again(js, web, monkeypatch):
    from app.jobsearch.models import RawPosting
    from app.jobsearch.postings import cleanse, raw_copies

    web.rows(BOARD, _row(1), _row(2))
    async with db.session_factory()() as session:
        site = await _board_site(session, _request())
        skill = await skills.current_skill(session, site.id)
        await _read(session)
        raws = list((await session.scalars(select(RawPosting).order_by(RawPosting.seq))).all())
        assert [(r.reader, r.reader_version, r.kind) for r in raws] == [(f"recipe:request:{skill.id}", 1, "item")] * 2
        monkeypatch.setattr(cleanse, "CLEANER_VERSION", cleanse.CLEANER_VERSION + 1)
        counts = await raw_copies.recleanse(session, OWNER)
        assert (counts["rebuilt"], counts["reparsed"], counts["stored"]) == (2, 2, {})
