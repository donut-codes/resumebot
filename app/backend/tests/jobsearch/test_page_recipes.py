"""Page recipes (the owner, 2026-10-22): steps and selectors recorded once in the app's own Chrome, then run nightly by
code over DevTools with no Claude session: the owner's target terms typed in the site's search box, the pages walked,
the postings listed, and a posting's page read for its text. Every site, page and word is made up; no test reaches the
network or a real browser: `FakeChrome` plays the page, answering each script by the marker it starts with."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser.recipes import live as recipe_live
from app.jobsearch.browser.recipes import page_run as page_recipe
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.models import Employer, JobPosting
from app.jobsearch.postings import site_reads
from app.sites import skills
from app.sites.models import Site, SiteRecipe, SiteRecording
from tests.builders import browser_world
from tests.support import DEFAULT_OWNER, OWNER

NOW = datetime(2026, 10, 23, 1, 0, tzinfo=UTC)
HOST = "careers.example.test"
START = f"https://{HOST}/search"
RECIPE = {"kind": "page", "url": START, "search": "input#q", "links": "a.job", "next": "button.next",
          "row": "li.row", "location": ".where", "text": "div#desc"}


def _recipe(**over) -> dict:
    return {**RECIPE, **over}


class FakeChrome:
    """The app's Chrome over DevTools, scripted. `results` maps a typed word (or "" for none) to the pages of rows it
    brings; each row is (title, href, place)."""

    def __init__(self, results: dict[str, list[list[tuple]]], status: int = 200, body: str = "x" * 600,
                 has_search: bool = True, tag: str = "INPUT", types: bool = True) -> None:
        self.tag, self.types = tag, types
        #: The links on a page opened by its address (a posting or a hub the recording samples), as (title, href).
        self.anchors_at: dict[str, list[tuple]] = {}
        self.results, self.status, self.body, self.has_search = results, status, body, has_search
        self.events: list[dict] | None = None
        self.calls: list[tuple[str, str]] = []
        self.params: list[tuple[str, dict]] = []
        self.typed: list[str] = []
        self.navigated: list[str] = []
        self.clicked: list[str] = []
        #: The labels of the controls in a cookie banner the page shows (None: none), and the numbers clicked.
        self.banner: list[str] | None = None
        self.declined: list[int] = []
        self.word = ""
        self.page = 0
        self.searched = False
        self.opened = False

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def _pages(self) -> list[list[tuple]]:
        return self.results.get(self.word if self.searched else "", [[]])

    async def call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        expression = (params or {}).get("expression", "")
        self.calls.append((method, expression[:24]))
        self.params.append((method, params or {}))
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Page.navigate":
            self.navigated.append(params["url"])
            self.word, self.page, self.searched = "", 0, False
            assert self.events is not None
            self.events += [{"method": "Network.requestWillBeSent", "params": {
                "requestId": f"d{len(self.navigated)}", "type": "Document",
                "request": {"method": "GET", "url": params["url"]}}},
                {"method": "Network.responseReceived", "params": {
                    "requestId": f"d{len(self.navigated)}", "type": "Document",
                    "response": {"status": self.status, "mimeType": "text/html"}}}]
            return {}
        if method != "Runtime.evaluate":
            return {}
        if expression == "document.readyState":
            return {"result": {"value": "complete"}}
        if expression.startswith("/*consent:find*/") and self.banner is not None:
            return {"result": {"value": json.dumps(self.banner)}}
        if expression.startswith("/*consent:click*/"):
            self.declined.append(int(expression.split('data-rb-consent="')[1].split('"')[0]))
            return {"result": {"value": json.dumps({"clicked": True})}}
        args = json.loads(expression.split("\n", 1)[0].split("//", 1)[1]) if expression.startswith("/*rb:") else []
        if expression.startswith("/*rb:has*/"):
            sel = args[0]
            here = {"input#q": self.has_search, "button.next": self.page < len(self._pages()) - 1,
                    "div#desc": True, "a.job": True}.get(sel, False)
            return {"result": {"value": json.dumps(here)}}
        if expression.startswith("/*rb:type*/"):
            self.word = args[1]
            self.typed.append(self.word)
            return {"result": {"value": json.dumps({"typed": self.types})}}
        if expression.startswith("/*rb:tag*/"):
            return {"result": {"value": json.dumps(self.tag)}}
        if expression.startswith("/*rb:click*/"):
            sel = args[0]
            self.clicked.append(sel)
            if sel == "button.next":
                self.page += 1
            else:
                self.searched, self.page = True, 0
            return {"result": {"value": json.dumps({"clicked": True})}}
        if expression.startswith("/*rb:rows*/"):
            pages = self._pages()
            rows = pages[min(self.page, len(pages) - 1)]
            return {"result": {"value": json.dumps([{"href": h, "title": t, "location": p} for t, h, p in rows])}}
        if expression.startswith("/*rb:anchors*/"):
            here = self.navigated[-1] if self.navigated else ""
            if here in self.anchors_at:
                rows = self.anchors_at[here]
            else:
                pages = self._pages()
                rows = pages[min(self.page, len(pages) - 1)]
            return {"result": {"value": json.dumps(
                [{"href": h, "raw": h, "title": t, "row": "li.row"} for t, h, *_ in rows])}}
        if expression.startswith("/*rb:description*/"):
            return {"result": {"value": json.dumps({"selector": "div#desc"})}}
        if expression.startswith("/*rb:text*/"):
            return {"result": {"value": json.dumps({"text": self.body})}}
        if expression.startswith("/*rb:html*/"):
            return {"result": {"value": json.dumps({"url": self.navigated[-1], "html": "<html>raw</html>"})}}
        return {"result": {"value": "1"}}


def row(n: int, place: str = "Springdale") -> tuple:
    return (f"Programs Director {n}", f"https://{HOST}/job/{n}", place)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(browser_code, "sleep", no_wait)
    monkeypatch.setattr(page_recipe, "SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(page_recipe, "FIND_SECONDS", 0.0)
    monkeypatch.setattr(recipe_record, "SETTLE_SECONDS", 0.0)

    async def allowed(self, url):
        return "disallowed" not in url

    monkeypatch.setattr(polite.Fetcher, "allows", allowed)


def chrome(monkeypatch, fake: FakeChrome) -> FakeChrome:
    monkeypatch.setattr(page_recipe, "make_tools", lambda: fake)
    return fake


# --- the recipe -----------------------------------------------------------------------------------------------------


def test_a_page_recipe_is_data_with_selectors_and_a_public_start():
    got = skills.parse_recipe(_recipe())
    assert got.kind == "page" and got.links == "a.job" and got.next == "button.next"


@pytest.mark.parametrize("field, value", [
    ("links", "a.job{color:red}"), ("search", "input; alert(1)"), ("next", "x" * 300), ("links", ""),
    ("text", "div</script>"), ("url", "http://localhost/jobs"), ("links", "a[href^=javascript:]"),
])
def test_a_page_recipe_with_a_selector_that_could_run_anything_or_a_private_start_is_refused(field, value):
    with pytest.raises(ValueError):
        skills.parse_recipe(_recipe(**{field: value}))


def test_a_site_read_never_runs_a_page_recipe_through_the_fetcher():
    assert "page" in site_reads.RUNNABLE and "page" in site_reads.LEARNED


# --- running it ------------------------------------------------------------------------------------------------------


async def test_each_of_the_owners_terms_is_typed_in_the_search_box_and_every_page_is_walked(monkeypatch):
    fake = chrome(monkeypatch, FakeChrome({"program manager": [[row(1), row(2)], [row(3)]],
                                           "operations": [[row(2, "Remote"), row(4)]]}))
    listing = await page_recipe.run(skills.parse_recipe(_recipe()), ["program manager", "operations"])
    assert fake.typed == ["program manager", "operations"]
    assert [p["title"] for p in listing.postings] == [f"Programs Director {n}" for n in (1, 2, 3, 4)]
    first = listing.postings[0]
    assert (first["url"], first["ext_id"], first["location"]) == (f"https://{HOST}/job/1", "/job/1", "Springdale")
    assert fake.clicked.count("button.next") == 1, "one next page, then none was there"
    assert fake.navigated == [START, START], "each term starts from the search page"


async def test_a_cookie_banner_on_the_page_is_declined_once_and_is_no_stop_and_the_terms_are_still_read(monkeypatch):
    """The owner, 2026-11-04: a cookie notice is never a block: deny the tracking cookies and move on."""
    fake = chrome(monkeypatch, FakeChrome({"program manager": [[row(1), row(2)]]}))
    fake.banner = ["Cookie settings", "Necessary only", "Accept all"]
    listing = await page_recipe.run(skills.parse_recipe(_recipe()), ["program manager"])
    assert fake.declined == [1], "the control that refuses the cookies that are not needed, never Accept"
    assert [p["title"] for p in listing.postings] == ["Programs Director 1", "Programs Director 2"]


async def test_a_banner_with_only_an_accept_option_is_left_alone_and_the_page_is_read_on(monkeypatch):
    fake = chrome(monkeypatch, FakeChrome({"program manager": [[row(1)]]}))
    fake.banner = ["Accept all", "OK"]
    listing = await page_recipe.run(skills.parse_recipe(_recipe()), ["program manager"])
    assert fake.declined == [] and len(listing.postings) == 1


async def test_a_search_by_terms_is_a_partial_read_that_closes_nothing(monkeypatch):
    chrome(monkeypatch, FakeChrome({"program manager": [[row(1)]]}))
    listing = await page_recipe.run(skills.parse_recipe(_recipe()), ["program manager"])
    assert listing.truncated is True and "program manager" in (listing.note or "")


async def test_with_no_terms_the_page_is_read_as_it_loads_and_nothing_is_typed(monkeypatch):
    fake = chrome(monkeypatch, FakeChrome({"": [[row(1), row(2)]]}))
    listing = await page_recipe.run(skills.parse_recipe(_recipe(search=None)), [])
    assert fake.typed == [] and len(listing.postings) == 2


async def test_nothing_is_clicked_but_the_recipes_own_search_and_next(monkeypatch):
    fake = chrome(monkeypatch, FakeChrome({"a": [[row(1)], [row(2)]]}))
    await page_recipe.run(skills.parse_recipe(_recipe()), ["a"])
    assert set(fake.clicked) <= {"input#q", "button.next"}


async def test_a_page_that_repeats_ends_the_walk_and_says_so(monkeypatch):
    chrome(monkeypatch, FakeChrome({"a": [[row(1), row(2)], [row(1), row(2)], [row(1), row(2)]]}))
    listing = await page_recipe.run(skills.parse_recipe(_recipe()), ["a"])
    assert len(listing.postings) == 2 and listing.truncated and "repeated" in (listing.note or "")


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_a_refusal_is_a_stop_that_cools_the_site(monkeypatch, status):
    chrome(monkeypatch, FakeChrome({"a": [[row(1)]]}, status=status))
    with pytest.raises(skills.SkillError) as stop:
        await page_recipe.run(skills.parse_recipe(_recipe()), ["a"])
    assert stop.value.status == status


async def test_a_start_that_robots_txt_disallows_is_never_loaded(monkeypatch):
    fake = chrome(monkeypatch, FakeChrome({"a": [[row(1)]]}))
    with pytest.raises(polite.Refused):
        await page_recipe.run(skills.parse_recipe(_recipe(url=f"https://{HOST}/disallowed")), ["a"])
    assert fake.navigated == []


async def test_a_link_off_the_site_or_to_a_sign_in_or_an_application_is_not_a_posting(monkeypatch):
    rows = [row(1), ("Elsewhere", "https://other.example.org/job/9", "Ohio"),
            ("Sign in", f"https://{HOST}/login", ""), ("Apply now", f"https://{HOST}/job/1/apply", "")]
    chrome(monkeypatch, FakeChrome({"a": [rows]}))
    listing = await page_recipe.run(skills.parse_recipe(_recipe()), ["a"])
    assert [p["url"] for p in listing.postings] == [f"https://{HOST}/job/1"]


async def test_a_link_to_the_rest_of_the_employers_site_off_the_careers_host_is_not_a_posting(monkeypatch):
    """A recipe on a careers host took the main site's menu ("Shop XBOX", "Gift cards") for postings,
    since they share the employer's domain. Only the careers host, or a host under it, keeps a link; a read refuses the
    rest."""
    rows = [row(1), ("Shop the store", "https://www.example.test/store/b/games", ""),
            ("Cloud products", "https://cloud.example.test/products", ""),
            ("Programs Lead 2", f"https://apply.{HOST}/job/2", "Ohio")]
    chrome(monkeypatch, FakeChrome({"a": [rows]}))
    listing = await page_recipe.run(skills.parse_recipe(_recipe()), ["a"])
    assert [p["url"] for p in listing.postings] == [f"https://{HOST}/job/1", f"https://apply.{HOST}/job/2"]
    fake = chrome(monkeypatch, FakeChrome({}, body="Five years of programs experience. " * 20))
    assert await page_recipe.read_posting(skills.parse_recipe(_recipe()), "https://www.example.test/store/b/x") is None
    assert fake.navigated == [], "a page off the careers host is never opened"
    # A start page on the employer's main site (www) covers the hosts under its domain.
    assert page_recipe.on_careers_host("https://jobs.example.test/1", "https://www.example.test/careers")


async def test_a_page_with_no_search_box_when_one_is_expected_is_a_changed_shape(monkeypatch):
    chrome(monkeypatch, FakeChrome({"a": [[row(1)]]}, has_search=False))
    with pytest.raises(skills.SkillError):
        await page_recipe.run(skills.parse_recipe(_recipe()), ["a"])


# --- the night's time (docs/PRD.md 28.17) -----------------------------------------------------------------------------


@pytest.fixture
def the_nights_clock(monkeypatch):
    """What a test of the night's time needs: a lock for the one Chrome of its own (a lock that has been waited for is
    bound to the event loop that waited), and a fetcher quick to make (a real one builds a TLS context, tens of
    milliseconds, which a deadline a fraction of a second away would count)."""
    monkeypatch.setattr(page_recipe, "_one_tab", asyncio.Lock())
    monkeypatch.setattr(polite, "GuardedTransport", lambda *a, **k: httpx.MockTransport(lambda r: httpx.Response(404)))


class SlowNext(FakeChrome):
    """A site whose next page takes a moment to come."""

    async def call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        if "/*rb:click*/" in (params or {}).get("expression", "") and "button.next" in (params or {}).get(
                "expression", ""):
            await asyncio.sleep(0.06)
        return await super().call(method, params, session_id)


async def test_a_run_waiting_for_the_one_chrome_gives_up_when_the_nights_time_runs_out(the_nights_clock, monkeypatch):
    """The app has one Chrome and the runs take turns at it (`_one_tab`). A site queued behind another waited as long
    as the one before it took, up to an hour each (`RUN_SECONDS`), however late in the night: the read begins many sites
    at once, so up to 24 could pass the night's check and then run one after another, hours past it. Old: it waited
    for its turn. New: it gives up when the night's time runs out, having read nothing (`NotBegun`)."""
    fake = chrome(monkeypatch, FakeChrome({"a": [[row(1)]]}))
    token = polite.READ_BY.set(time.monotonic() + 0.05)
    try:
        async with page_recipe._one_tab:  # another site has the app's Chrome
            began = time.monotonic()
            with pytest.raises(page_recipe.NotBegun):
                await page_recipe.run(skills.parse_recipe(_recipe()), ["a"])
            waited = time.monotonic() - began
    finally:
        polite.READ_BY.reset(token)
    assert waited < 1.0 and fake.navigated == []
    assert not page_recipe._one_tab.locked(), "the turn it gave up was not taken, nor the other's kept"


async def test_a_run_whose_turn_comes_after_the_nights_time_is_not_begun(the_nights_clock, monkeypatch):
    fake = chrome(monkeypatch, FakeChrome({"a": [[row(1)]]}))
    token = polite.READ_BY.set(time.monotonic() - 1)
    try:
        with pytest.raises(page_recipe.NotBegun):
            await page_recipe.run(skills.parse_recipe(_recipe()), ["a"])
    finally:
        polite.READ_BY.reset(token)
    assert fake.navigated == [] and not page_recipe._one_tab.locked()


async def test_a_run_with_time_left_in_the_night_reads_as_it_always_did(the_nights_clock, monkeypatch):
    chrome(monkeypatch, FakeChrome({"a": [[row(1), row(2)]]}))
    token = polite.READ_BY.set(time.monotonic() + 3600)
    try:
        listing = await page_recipe.run(skills.parse_recipe(_recipe()), ["a"])
    finally:
        polite.READ_BY.reset(token)
    assert len(listing.postings) == 2 and "time" not in (listing.note or "")


async def test_a_run_stops_walking_pages_when_the_nights_time_runs_out_not_in_its_hour(the_nights_clock, monkeypatch):
    """Old: a run was given `RUN_SECONDS` whatever was left of the night, so a site begun at the last minute went on
    for an hour. New: its time is the shorter of the two, and a run cut by the night says so and keeps the pages it
    read."""
    fake = chrome(monkeypatch, SlowNext({"a": [[row(n)] for n in range(1, 7)]}))
    token = polite.READ_BY.set(time.monotonic() + 0.05)
    try:
        listing = await page_recipe.run(skills.parse_recipe(_recipe()), ["a"])
    finally:
        polite.READ_BY.reset(token)
    assert 1 <= len(listing.postings) < 6 and "the time allowed for the read ran out" in (listing.note or "")
    assert fake.clicked.count("button.next") < 5


# --- a posting's page ------------------------------------------------------------------------------------------------


async def test_a_postings_text_is_the_recipes_text_element_with_the_page_kept_as_the_raw_copy(monkeypatch):
    fake = chrome(monkeypatch, FakeChrome({}, body="Five years of programs experience. " * 20))
    got = await page_recipe.read_posting(skills.parse_recipe(_recipe()), f"https://{HOST}/job/1")
    assert got is not None and got["text"].startswith("Five years") and got["raw"] == b"<html>raw</html>"
    assert fake.navigated == [f"https://{HOST}/job/1"]


async def test_a_posting_page_robots_txt_disallows_is_not_opened(monkeypatch):
    fake = chrome(monkeypatch, FakeChrome({}))
    with pytest.raises(polite.Refused):
        await page_recipe.read_posting(skills.parse_recipe(_recipe()), f"https://{HOST}/disallowed/1")
    assert fake.navigated == []


async def test_a_recipe_with_no_text_element_reads_no_posting(monkeypatch):
    chrome(monkeypatch, FakeChrome({}))
    assert await page_recipe.read_posting(skills.parse_recipe(_recipe(text=None)), f"https://{HOST}/job/1") is None


# --- the night -------------------------------------------------------------------------------------------------------


async def _live_site(employer: bool = False) -> tuple[Site, SiteRecipe]:
    """A site read by its live page recipe; with `employer`, the site of an employer that has 3 open roles."""
    async with db.session_factory()() as session:
        initech = await employers.find_or_make(session, OWNER, "Initech") if employer else None
        if initech is not None:
            initech.open_roles = 3
        site = Site(owner=OWNER, url=START, kind="employer", read_by="app", created_at=NOW - timedelta(days=1),
                    employer_id=initech.id if initech is not None else None)
        session.add(site)
        await session.flush()
        row_ = SiteRecipe(owner=OWNER, site_id=site.id, version=1, recipe=_recipe(), state="live", created_at=NOW)
        session.add(row_)
        await session.commit()
        return site, row_


async def test_the_nightly_read_runs_a_live_page_recipe_in_chrome_and_puts_its_postings_in_the_index(js, monkeypatch):
    await browser_world(extra_sites=False)
    await _live_site()
    fake = chrome(monkeypatch, FakeChrome({"": [[row(1), row(2)]]}))
    async with db.session_factory()() as session:
        _built_in, reads = await site_reads.plan(session, OWNER, [])
        assert [kind for kind in (site_reads.kind_of(r.skill) for r in reads)] == ["page"]
        fetch = polite.Fetcher()
        try:
            counts = await site_reads.refresh(session, OWNER, fetch, reads, NOW)
        finally:
            await fetch.close()
        await session.commit()
    assert counts["new"] == 2 and counts["failed"] == 0
    async with db.session_factory()() as session:
        titles = sorted(p.title for p in (await session.scalars(select(JobPosting))).all())
    assert titles == ["Programs Director 1", "Programs Director 2"] and fake.navigated == [START]


async def test_a_page_site_whose_turn_never_came_before_the_nights_time_is_out_is_unread_not_failed(
        js, the_nights_clock, monkeypatch):
    """Docs/PRD.md 28.17: a site the night's time ran out on while it waited for the one Chrome is left for the next
    read, like a board not begun: counted `unread`, no fault, its recipe untouched (a fault would retire it)."""
    await browser_world(extra_sites=False)
    site, live = await _live_site()
    fake = chrome(monkeypatch, FakeChrome({"": [[row(1), row(2)]]}))
    token = polite.READ_BY.set(time.monotonic() + 0.3)
    try:
        async with db.session_factory()() as session:
            _built_in, reads = await site_reads.plan(session, OWNER, [])
            fetch = polite.Fetcher()
            try:
                async with page_recipe._one_tab:  # another site has the app's Chrome until the night is over
                    counts = await asyncio.wait_for(site_reads.refresh(session, OWNER, fetch, reads, NOW), 10)
            finally:
                await fetch.close()
            await session.commit()
    finally:
        polite.READ_BY.reset(token)
    assert (counts["unread"], counts["failed"], counts["faults"], counts["new"]) == (1, 0, [], 0)
    assert fake.navigated == []
    async with db.session_factory()() as session:
        kept = await session.get(SiteRecipe, live.id)
        assert kept is not None and kept.state == "live" and kept.retired_at is None and not kept.bad_nights


class RunsTheNightOut(FakeChrome):
    """A site whose first page brings the night's time to its end (the night's fetcher `fetch` has no time left)."""

    def __init__(self, fetch: polite.Fetcher, *a, **k) -> None:
        super().__init__(*a, **k)
        self.fetch = fetch

    async def call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        if "/*rb:rows*/" in (params or {}).get("expression", ""):
            self.fetch.deadline = time.monotonic() - 1
        return await super().call(method, params, session_id)


async def _health_of_6(live: SiteRecipe) -> list[dict]:
    """Three earlier nights of six postings each (the recent median a night is judged against), kept on the recipe."""
    health = [{"at": (NOW - timedelta(days=n)).isoformat(), "rows": 6, "requests": 3, "details": 0, "fill": None}
              for n in (3, 2, 1)]
    async with db.session_factory()() as session:
        row_ = await session.get(SiteRecipe, live.id)
        assert row_ is not None
        row_.health = health
        await session.commit()
    return health


async def _a_night(monkeypatch, pages: list[list[tuple]], clock_runs_out: bool) -> dict:
    """One night's read of the page recipe; what `refresh` returns. With `clock_runs_out` the first page brings the
    night's time to its end and the run stops there (its page limit stands in for its own clock, which a test cannot
    hold still)."""
    async with db.session_factory()() as session:
        _built_in, reads = await site_reads.plan(session, OWNER, [])
        fetch = polite.Fetcher()
        if clock_runs_out:
            monkeypatch.setattr(page_recipe, "PAGES_AT_MOST", 1)
            chrome(monkeypatch, RunsTheNightOut(fetch, {"": pages}))
        else:
            chrome(monkeypatch, FakeChrome({"": pages}))
        try:
            counts = await site_reads.refresh(session, OWNER, fetch, reads, NOW)
        finally:
            await fetch.close()
        await session.commit()
    return counts


async def test_a_page_run_cut_short_by_the_nights_time_is_no_bad_night_and_its_postings_are_kept(
        js, the_nights_clock, monkeypatch):
    """Docs/PRD.md 28.17. Old: the run's few rows were judged against the recipe's recent median ("it read 2 postings
    against a recent median of 6"), so a healthy recipe whose turn came late was a bad night, never stored, and stale
    after a second one. New: a read cut by the night's time is counted `cut`, its postings are stored, and the
    recipe's health is as it was."""
    await browser_world(extra_sites=False)
    _site, live = await _live_site()
    health = await _health_of_6(live)
    counts = await _a_night(monkeypatch, [[row(1), row(2)], [row(3), row(4)]], clock_runs_out=True)
    assert (counts["cut"], counts["new"], counts["failed"], counts["faults"]) == (1, 2, 0, [])
    async with db.session_factory()() as session:
        kept = await session.get(SiteRecipe, live.id)
        assert kept is not None and (kept.state, kept.bad_nights, kept.retired_at) == ("live", 0, None)
        assert kept.health == health, "a night that was cut says nothing of the recipe's health"
        titles = sorted(p.title for p in (await session.scalars(select(JobPosting))).all())
    assert titles == ["Programs Director 1", "Programs Director 2"]


async def test_a_page_run_cut_before_it_read_a_posting_is_no_empty_board_and_changes_nothing(
        js, the_nights_clock, monkeypatch):
    """Docs/PRD.md 28.17: a read cut short says nothing of the site. One cut before it read a posting is a board not
    read, not a board that answered none: no fault (it would retire the site's skill and mark the employer's board
    empty), no bad night, and the employer's record as it was."""
    await browser_world(extra_sites=False)
    site, live = await _live_site(employer=True)
    health = await _health_of_6(live)
    counts = await _a_night(monkeypatch, [[], [row(1)]], clock_runs_out=True)
    assert (counts["unread"], counts["cut"], counts["failed"], counts["faults"], counts["new"]) == (1, 0, 0, [], 0)
    async with db.session_factory()() as session:
        kept = await session.get(SiteRecipe, live.id)
        assert kept is not None and (kept.state, kept.bad_nights, kept.health) == ("live", 0, health)
        initech = await session.get(Employer, site.employer_id)
        assert initech is not None
        assert (initech.open_roles, initech.fetch_ok, initech.fetch_kind, initech.fetched_at) == (3, None, None, None)


async def test_a_read_cut_short_by_a_repeated_page_and_not_the_clock_is_still_judged_against_the_median(
        js, the_nights_clock, monkeypatch):
    """The night's time is what excuses a short read, not the read being partial: a walk that ended on a repeated page
    with time to spare, and read 2 postings against a recent median of 6, is a bad night as it was."""
    await browser_world(extra_sites=False)
    _site, live = await _live_site()
    await _health_of_6(live)
    counts = await _a_night(monkeypatch, [[row(1), row(2)]] * 3, clock_runs_out=False)
    assert (counts["cut"], counts["new"], counts["failed"]) == (0, 0, 1)
    async with db.session_factory()() as session:
        kept = await session.get(SiteRecipe, live.id)
    assert kept is not None and kept.bad_nights == 1
    assert kept.health[-1]["why"] == "it read 2 postings against a recent median of 6"


async def test_two_nights_that_read_nothing_make_a_page_recipe_stale_and_ask_a_re_record(js, monkeypatch):
    await browser_world(extra_sites=False)
    site, live = await _live_site()
    chrome(monkeypatch, FakeChrome({"": [[]]}))
    for night in (0, 1):
        async with db.session_factory()() as session:
            _built_in, reads = await site_reads.plan(session, OWNER, [])
            fetch = polite.Fetcher()
            try:
                await site_reads.refresh(session, OWNER, fetch, reads, NOW + timedelta(days=night))
            finally:
                await fetch.close()
            await session.commit()
    async with db.session_factory()() as session:
        assert (await session.get(SiteRecipe, live.id)).state == "stale"
        asked = (await session.scalars(select(SiteRecording).where(SiteRecording.site_id == site.id))).all()
    assert [(r.reason, r.state) for r in asked] == [("repair", "requested")]


# --- reading a posting by code ----------------------------------------------------------------------------------------


async def test_a_page_recipes_host_has_its_postings_read_by_code_with_the_recipes_text_element(js, monkeypatch):
    await browser_world(extra_sites=False)
    await _live_site()
    chrome(monkeypatch, FakeChrome({}, body="Five years of programs experience. " * 20))
    async with db.session_factory()() as session:
        found = await recipe_live.page_recipe_for(session, OWNER, f"https://{HOST}/job/7")
        assert found is not None and found.recipe["links"] == "a.job"
        assert await recipe_live.page_recipe_for(session, OWNER, "https://other.example.org/job/7") is None
    job = browser_code.Job(item_id="i", kind=bp.READ, link=f"https://{HOST}/job/7", domain="example.test", title=None,
                           named=False)
    job.page_recipe = found.recipe

    async def turn():
        return None

    got = await browser_code.read_by_code(job, frozenset(), turn)
    assert got is not None and got["result"] == "text" and got["raw"] == b"<html>raw</html>"


# --- recording -------------------------------------------------------------------------------------------------------


@pytest.fixture
def steps(monkeypatch):
    """The finders of a visitor's steps, and the selectors the page gives them (the page itself is `FakeChrome`)."""
    found = {"search": {"found": True, "what": "search box", "href": None},
             "next": {"found": True, "what": "next page", "href": None}}
    picked = iter(["input#q", "button.next"])

    async def mark(_tab, step):
        return found.get(step)

    async def selector(_tab):
        return next(picked)

    monkeypatch.setattr(page_recipe, "_mark", mark)
    monkeypatch.setattr(page_recipe, "_selector", selector)
    return found


async def _asked(site_url: str = START, reason: str = "page") -> Site:
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url=site_url, kind="employer", read_by="app", created_at=NOW - timedelta(days=1))
        session.add(site)
        await session.flush()
        await recipe_record.request(session, OWNER, site, reason, NOW)
        await session.commit()
        return site


async def _ask_page_recording(ref: str) -> None:
    """Ask for a page recording of the owner's site that `ref` names (an address or a host), as the owner action
    does."""
    async with db.session_factory()() as session:
        site = await recipe_record.find_site(session, DEFAULT_OWNER, ref)
        assert site is not None
        await recipe_record.request(session, DEFAULT_OWNER, site, "page")
        await session.commit()


async def test_a_page_recording_is_asked_for_when_told_a_site_draws_itself(js):
    async with db.session_factory()() as session:
        session.add(Site(owner=DEFAULT_OWNER, url=START, kind="employer", read_by="app", created_at=NOW))
        await session.commit()
    await _ask_page_recording(START)
    async with db.session_factory()() as session:
        rec = await session.scalar(select(SiteRecording))
    assert (rec.reason, rec.state) == ("page", "requested")


async def test_a_page_recording_is_recorded_run_once_for_real_and_kept_live(js, monkeypatch, steps):
    await browser_world(extra_sites=False)
    site = await _asked()
    fake = chrome(monkeypatch, FakeChrome({"": [[row(1), row(2), row(3)]]}))
    got = await page_recipe.record_by_code(OWNER, await _rec_id(site))
    assert got["result"] == bp.FINISHED and "3 postings" in got["note"]
    async with db.session_factory()() as session:
        live = (await session.scalars(select(SiteRecipe).where(SiteRecipe.site_id == site.id))).all()
        rec = await session.scalar(select(SiteRecording))
    assert len(live) == 1 and live[0].state == "live" and rec.state == "written"
    kept = live[0].recipe
    assert (kept["kind"], kept["search"], kept["links"], kept["next"], kept["text"]) == (
        "page", "input#q", 'a[href*="/job/"]', "button.next", "div#desc")
    assert kept["provenance"]["tested_on"] == 3
    assert set(fake.clicked) <= {"input#q", "button.next"}


async def test_a_recordings_test_run_stops_once_it_has_read_enough_instead_of_walking_the_whole_site(
        js, monkeypatch, steps):
    """Found when recordings of several sites all hit the 120-second limit of a code-first try: the
    test run walked every result page (up to 400, a second and a settle each) to prove a recipe that needs 3 rows."""
    await browser_world(extra_sites=False)
    site = await _asked()
    pages = [[row(3 * i + 1), row(3 * i + 2), row(3 * i + 3)] for i in range(60)]
    fake = chrome(monkeypatch, FakeChrome({"": pages}))
    got = await page_recipe.record_by_code(OWNER, await _rec_id(site))
    assert got["result"] == bp.FINISHED and "a page recipe was recorded" in got["note"]
    assert fake.clicked.count("button.next") <= 1, "three rows were enough to prove it; no page walk after that"


async def test_a_recording_that_reads_too_few_postings_is_kept_by_no_one_and_says_why(js, monkeypatch, steps):
    await browser_world(extra_sites=False)
    site = await _asked()
    fake = chrome(monkeypatch, FakeChrome({"": [[row(1)]]}))
    fake.anchors_at[START] = [row(1), row(2), row(3)]  # the page as recorded showed three; the run reads one
    got = await page_recipe.record_by_code(OWNER, await _rec_id(site))
    assert got["result"] == bp.FINISHED and "1 postings" in got["note"]
    async with db.session_factory()() as session:
        assert (await session.scalars(select(SiteRecipe))).all() == []
        rec = await session.scalar(select(SiteRecording))
    assert rec.state == "failed"


async def test_a_recording_on_a_site_that_refuses_is_a_block_and_keeps_nothing(js, monkeypatch, steps):
    await browser_world(extra_sites=False)
    site = await _asked()
    chrome(monkeypatch, FakeChrome({"": [[row(1)]]}, status=403))
    got = await page_recipe.record_by_code(OWNER, await _rec_id(site))
    assert got["result"] == "blocked"
    async with db.session_factory()() as session:
        assert (await session.scalars(select(SiteRecipe))).all() == []


async def test_the_record_item_of_a_site_that_had_a_page_recipe_is_recorded_by_steps_again(js, monkeypatch, steps):
    await browser_world(extra_sites=False)
    site = await _asked(reason="repair")
    async with db.session_factory()() as session:
        session.add(SiteRecipe(owner=OWNER, site_id=site.id, version=1, recipe=_recipe(), state="stale",
                               created_at=NOW))
        await session.commit()
    chrome(monkeypatch, FakeChrome({"": [[row(1), row(2), row(3)]]}))
    got = await recipe_record.record_by_code(OWNER, HOST)
    assert got["result"] == bp.FINISHED
    async with db.session_factory()() as session:
        versions = (await session.scalars(select(SiteRecipe).order_by(SiteRecipe.version))).all()
    assert [(v.version, v.state) for v in versions] == [(1, "retired"), (2, "live")]


async def _rec_id(site: Site):
    async with db.session_factory()() as session:
        return (await session.scalar(select(SiteRecording).where(SiteRecording.site_id == site.id))).id


# --- the owner's Chrome ----------------------------------------------------------------------------------------------


def _only_own_window(fake: FakeChrome) -> None:
    made = [p for m, p in fake.params if m == "Target.createTarget"]
    assert made and all(p.get("newWindow") is True for p in made), "a window of its own, never a tab of the owner's"
    closed = [p["targetId"] for m, p in fake.params if m == "Target.closeTarget"]
    assert closed == ["t1"], "only the tab it made is closed"
    ended = {"Browser.close", "Browser.crash", "Target.disposeBrowserContext", "Browser.resetPermissions"}
    assert not ended & {m for m, _ in fake.params}, "Chrome is never ended or restarted"


async def test_a_run_works_in_a_window_of_its_own_closes_only_its_tab_and_never_ends_chrome(monkeypatch):
    fake = chrome(monkeypatch, FakeChrome({"a": [[row(1)]]}))
    await page_recipe.run(skills.parse_recipe(_recipe()), ["a"])
    _only_own_window(fake)


async def test_reading_a_posting_works_in_a_window_of_its_own_and_closes_only_its_tab(monkeypatch):
    fake = chrome(monkeypatch, FakeChrome({}, body="Five years of programs experience. " * 20))
    await page_recipe.read_posting(skills.parse_recipe(_recipe()), f"https://{HOST}/job/1")
    _only_own_window(fake)


async def test_a_recording_works_in_a_window_of_its_own_and_closes_only_its_tab(monkeypatch):
    fake = FakeChrome({})
    await recipe_record.capture_page(START, tools=fake, interact=False)
    _only_own_window(fake)


async def test_a_recording_of_steps_works_in_a_window_of_its_own_even_when_it_fails(monkeypatch, steps):
    fake = chrome(monkeypatch, FakeChrome({"": [[row(1)]]}, status=403))
    await page_recipe.propose(START, None)
    _only_own_window(fake)


# --- the owner's own request, and a bot check -------------------------------------------------------------------------


async def test_a_page_recording_the_owner_asked_for_is_taken_at_any_hour_and_a_repair_is_not(js):
    first = await _asked(reason="page")
    async with db.session_factory()() as session:
        other = Site(owner=OWNER, url="https://jobs.example.test/search", kind="employer", read_by="app",
                     created_at=NOW)
        session.add(other)
        await session.flush()
        await recipe_record.request(session, OWNER, other, "repair", NOW)
        await session.commit()
        wanted = {w["link"]: w["priority"] for w in await recipe_record.wanted(session, OWNER)}
    assert wanted == {first.url: 0, "https://jobs.example.test/search": 1}


async def test_a_page_that_answers_a_bot_check_is_a_stop_in_a_run_a_reading_and_a_recording(monkeypatch, steps):
    chrome(monkeypatch, FakeChrome({"a": [[row(1)]]}, status=202))
    with pytest.raises(skills.SkillError) as stop:
        await page_recipe.run(skills.parse_recipe(_recipe()), ["a"])
    assert stop.value.status == 202
    with pytest.raises(skills.SkillError):
        await page_recipe.read_posting(skills.parse_recipe(_recipe()), f"https://{HOST}/job/1")
    assert await page_recipe.propose(START, None) == (None, None, 202)


async def test_a_button_the_finder_marks_as_the_search_box_is_no_search_box_and_the_page_is_read_as_it_loads(
        monkeypatch, steps):
    chrome(monkeypatch, FakeChrome({"": [[row(1), row(2), row(3)]]}, tag="BUTTON"))
    recipe, why, status = await page_recipe.propose(START, "program manager")
    assert recipe is not None and "search" not in recipe and (why, status) == (None, None)


async def test_a_search_box_that_cannot_be_typed_in_is_left_out_of_the_recipe(monkeypatch, steps):
    chrome(monkeypatch, FakeChrome({"": [[row(1), row(2), row(3)]]}, types=False))
    recipe, why, status = await page_recipe.propose(START, "program manager")
    assert recipe is not None and "search" not in recipe and (why, status) == (None, None)


class _RefusesFirstAttach(FakeChrome):
    """The bridge refusing, once, the attach to a tab it had just opened (a recorded bridge refusal)."""

    def __init__(self, refusals: int = 1) -> None:
        super().__init__({"a": [[row(1)]]})
        self.refusals, self.made = refusals, 0

    async def call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        if method == "Target.createTarget":
            self.made += 1
            self.params.append((method, params or {}))
            return {"targetId": f"t{self.made}"}
        if method == "Target.attachToTarget" and self.refusals:
            self.refusals -= 1
            self.params.append((method, params or {}))
            raise browser_code.BrowserUnavailable("Target.attachToTarget: not a tab this session opened")
        return await super().call(method, params, session_id)


async def test_a_tab_the_bridge_refuses_to_attach_once_is_opened_again(monkeypatch):
    fake = chrome(monkeypatch, _RefusesFirstAttach())
    async with page_recipe.tab() as t:
        assert t.sid == "s1"
    made = [p for m, p in fake.params if m == "Target.createTarget"]
    assert len(made) == 2 and all(p.get("newWindow") is True for p in made), "a fresh tab, in the app's own window"
    assert [p["targetId"] for m, p in fake.params if m == "Target.closeTarget"] == ["t1", "t2"], "its own tabs only"


async def test_a_tab_refused_twice_is_given_up_as_the_browser_being_unavailable(monkeypatch):
    chrome(monkeypatch, _RefusesFirstAttach(refusals=2))
    with pytest.raises(browser_code.BrowserUnavailable, match="not a tab this session opened"):
        async with page_recipe.tab():
            pass


# --- which links are the postings ----------------

GUIDS = ["0f8fad5b-d9cb-469f-a165-70867728950e", "7c9e6679-7425-40de-944b-e07fc1f90ae7",
         "1b4e28ba-2fa1-11d2-883f-0016d3cca427", "6fa459ea-ee8a-3ca4-894e-db77e160355e",
         "16fd2706-8baf-433b-82eb-8c7fada847da", "886313e1-3b8a-5372-9b90-0c9aee199e5d",
         "a8098c1a-f86e-11da-bd1a-00112444be1e"]


def _a(title: str, path: str) -> tuple:
    return (title, f"https://{HOST}{path}", None)


#: A board whose team pages outnumber its postings (`/teams/<team>` beside `/profile/job_details/<id>`).
TEAMS_AND_POSTINGS = ([_a(f"{t} team", f"/teams/{t.lower()}") for t in
                       ("Design", "Research", "Infrastructure", "Sales", "Marketing", "Legal", "Finance", "People")]
                      + [_a(f"Programs Director {n}", f"/profile/job_details/{4810 + n}") for n in range(4)])
#: A board whose job-family pages outnumber its postings (`/job-family/<f>` beside `/<town>/<slug>/<GUID>/job`).
FAMILIES_AND_POSTINGS = ([_a(f"{f} careers", f"/job-family/{f.lower()}") for f in
                          ("Engineering", "Finance", "Operations", "Legal", "Markets", "People")]
                         + [_a(f"Programs Lead {n}", f"/{town}/programs-lead-{n}/{GUIDS[n]}/job")
                            for n, town in enumerate(("springdale-xx", "riverton-yy", "lakeside-zz", "springdale-xx"))])
#: A board where most postings on the first page are in one town (`/springdale-xx/...` five times, another town twice).
ONE_TOWN_MOSTLY = [_a(f"Analyst {n}", f"/{'springdale-xx' if n < 5 else 'riverton-yy'}/analyst-{n}/{GUIDS[n]}/job")
                   for n in range(7)]
#: Every posting on the first page in one town, under a common word (`/job/<town>/<slug>/<id>`).
ONE_TOWN_ONLY = [_a(f"Analyst {n}", f"/job/springdale-xx/analyst-{n}/{70120 + n}") for n in range(4)]


def _best(rows: list[tuple]) -> str:
    groups = page_recipe.link_groups([{"href": h, "raw": h, "title": t, "row": "li.row"} for t, h, _ in rows])
    return groups[0]["selector"]


def test_posting_links_with_an_id_and_a_posting_word_win_over_more_numerous_team_pages():
    assert _best(TEAMS_AND_POSTINGS) == 'a[href*="/job_details/"]'


def test_posting_links_ending_in_a_guid_and_job_win_over_more_numerous_job_family_pages():
    assert _best(FAMILIES_AND_POSTINGS) == 'a[href$="/job"]'


def test_the_selector_is_the_common_pattern_not_the_town_most_postings_are_in():
    assert _best(ONE_TOWN_MOSTLY) == 'a[href$="/job"]'
    assert _best(ONE_TOWN_ONLY) == 'a[href*="/job/"]'


@pytest.mark.parametrize("href, shaped", [
    (f"https://{HOST}/profile/job_details/481234", True), (f"https://{HOST}/riverton-yy/lead/{GUIDS[0]}/job", True),
    (f"https://{HOST}/careers/FolderDetail/Programs-Lead/43690", True), (f"https://{HOST}/job/1", True),
    (f"https://{HOST}/en/jobs/10551375/programs-lead", True), (f"https://{HOST}/search?jobId=481234", True),
    (f"https://{HOST}/teams/design", False), (f"https://{HOST}/job-family/finance", False),
    (f"https://{HOST}/v2/global/en/life-at-example/benefits.html", False), (f"https://{HOST}/en/impressum", False),
    (f"https://{HOST}/careers/Job-Search_US?listFilterMode=true&11760%5B0%5D=27877240", False),
    (f"https://{HOST}/category/finance-jobs/30291/8842/1", False), (f"https://{HOST}/jobs", False)])
def test_a_posting_shaped_address_names_one_posting(href, shaped):
    assert page_recipe.posting_shaped(href) is shaped


async def test_a_recording_takes_the_posting_links_not_the_team_pages(monkeypatch, steps):
    chrome(monkeypatch, FakeChrome({"": [TEAMS_AND_POSTINGS]}))
    recipe, why, status = await page_recipe.propose(START, None)
    assert (why, status) == (None, None) and recipe is not None
    assert recipe["links"] == 'a[href*="/job_details/"]' and recipe["text"] == "div#desc"


async def test_a_group_whose_opened_links_list_jobs_is_no_group_of_postings(monkeypatch, steps):
    """The page lists only category pages that look like postings by their word (`/job/<category>`); each one opened
    lists many postings of its own, so it is a page that lists jobs, and no recipe is made of it."""
    categories = [_a(f"{c} jobs", f"/job/{c.lower()}") for c in ("Engineering", "Finance", "Operations", "Legal")]
    fake = chrome(monkeypatch, FakeChrome({"": [categories]}))
    for _title, href, _ in categories:
        fake.anchors_at[href] = [_a(f"Programs Lead {n}", f"/job/programs-lead/{90000 + n}") for n in range(6)]
    recipe, why, status = await page_recipe.propose(START, None)
    assert recipe is None and status is None and "not to single postings" in (why or "")
    assert fake.navigated[1:] == [categories[0][1]], "one opened link was enough to tell"


async def test_a_run_never_keeps_a_link_that_is_not_posting_shaped(monkeypatch):
    rows = [row(1), ("Benefits", f"https://{HOST}/en/life/benefits.html", None),
            ("Design team", f"https://{HOST}/teams/design", None), row(2)]
    chrome(monkeypatch, FakeChrome({"": [rows]}))
    listing = await page_recipe.run(skills.parse_recipe(_recipe(search=None)), [])
    assert [p["url"] for p in listing.postings] == [f"https://{HOST}/job/1", f"https://{HOST}/job/2"]


# --- retiring a site's recipe -----------------------------------------------------------------------------------------


async def test_a_retired_recipe_is_kept_as_history_out_of_force_and_the_next_recording_is_fresh(js, monkeypatch, steps):
    from app.models.work import TrailEvent

    await browser_world(extra_sites=False)
    async with db.session_factory()() as session:
        site = Site(owner=DEFAULT_OWNER, url=START, kind="employer", read_by="app", created_at=NOW - timedelta(days=1))
        session.add(site)
        await session.flush()
        session.add(SiteRecipe(owner=DEFAULT_OWNER, site_id=site.id, version=1, created_at=NOW, state="live",
                               recipe=_recipe(links='a[href*="/teams/"]')))
        await session.commit()
    async with db.session_factory()() as session:
        site = await recipe_record.find_site(session, DEFAULT_OWNER, HOST)
        retired = await recipe_record.retire(session, DEFAULT_OWNER, site, "it read team pages")
        assert [r.version for r in retired] == [1]
        await session.commit()
    async with db.session_factory()() as session:
        old = await session.scalar(select(SiteRecipe))
        line = await session.scalar(select(TrailEvent).where(TrailEvent.event == recipe_record.RETIRED_EVENT))
        _built_in, reads = await site_reads.plan(session, DEFAULT_OWNER, [])
    assert (old.state, old.retired_at is not None) == ("retired", True)
    assert line is not None and "it read team pages" in line.sentence
    assert not any(isinstance(r.skill, SiteRecipe) for r in reads), "a retired recipe reads nothing"
    async with db.session_factory()() as session:
        site = await recipe_record.find_site(session, DEFAULT_OWNER, HOST)
        assert await recipe_record.retire(session, DEFAULT_OWNER, site, "again") == []  # nothing left in force
        await session.commit()
    await _ask_page_recording(HOST)

    async def no_terms(_session, _owner):
        return []

    monkeypatch.setattr(page_recipe, "terms_of", no_terms)
    chrome(monkeypatch, FakeChrome({"": [[row(1), row(2), row(3)]]}))
    async with db.session_factory()() as session:
        rec_id = (await session.scalar(select(SiteRecording).where(SiteRecording.site_id == site.id))).id
    got = await page_recipe.record_by_code(DEFAULT_OWNER, rec_id)
    assert got["result"] == bp.FINISHED
    async with db.session_factory()() as session:
        versions = (await session.scalars(select(SiteRecipe).order_by(SiteRecipe.version))).all()
    assert [(v.version, v.state, v.recipe["links"]) for v in versions] == [
        (1, "retired", 'a[href*="/teams/"]'), (2, "live", 'a[href*="/job/"]')]


async def test_a_site_whose_page_recipe_was_retired_is_not_a_page_site_but_a_stale_one_is(js):
    await browser_world(extra_sites=False)
    async with db.session_factory()() as session:
        retired = Site(owner=OWNER, url=START, kind="employer", read_by="app", created_at=NOW - timedelta(days=1))
        stale = Site(owner=OWNER, url="https://other.example.test/search", kind="employer", read_by="app",
                     created_at=NOW - timedelta(days=1))
        session.add_all([retired, stale])
        await session.flush()
        session.add_all([
            SiteRecipe(owner=OWNER, site_id=retired.id, version=1, recipe=_recipe(), state="retired",
                       retired_at=NOW, created_at=NOW),
            SiteRecipe(owner=OWNER, site_id=stale.id, version=1, recipe=_recipe(), state="stale", created_at=NOW)])
        await session.commit()
        assert await page_recipe.is_page_site(session, retired.id) is False
        assert await page_recipe.is_page_site(session, stale.id) is True


async def test_a_page_recipe_for_a_job_systems_portal_with_a_public_id_in_its_path_is_recorded_kept_and_read(
        js, monkeypatch, steps):
    """A job system's pages carry the employer's public id (a 32-hex key) in the path (docs/PRD.md 30.3): the address
    is no token, so the recording is kept and the nightly read lists the postings."""
    key = "0123456789abcdef" * 2
    portal = f"https://{HOST}/v4/ats/web.php/portal/{key}/career-page"
    jobs = [(f"Programs Director {n}", f"https://{HOST}/v4/ats/web.php/portal/{key}/jobs/{100 + n}", "Springdale")
            for n in (1, 2, 3)]
    await browser_world(extra_sites=False)
    site = await _asked(portal)
    chrome(monkeypatch, FakeChrome({"": [jobs]}))
    got = await page_recipe.record_by_code(OWNER, await _rec_id(site))
    assert got["result"] == bp.FINISHED and "3 postings" in got["note"]
    async with db.session_factory()() as session:
        [live] = (await session.scalars(select(SiteRecipe).where(SiteRecipe.site_id == site.id))).all()
    assert live.state == "live" and live.recipe["url"] == portal
    fake = chrome(monkeypatch, FakeChrome({"": [jobs]}))
    async with db.session_factory()() as session:
        _built_in, reads = await site_reads.plan(session, OWNER, [])
        fetch = polite.Fetcher()
        try:
            counts = await site_reads.refresh(session, OWNER, fetch, reads, NOW)
        finally:
            await fetch.close()
        await session.commit()
    assert counts["new"] == 3 and counts["failed"] == 0 and fake.navigated[0] == portal
