"""HTML recipes (docs/PRD.md path 49): a site whose postings are in the page's own HTML is read by selectors recorded
from the rendered page and run on a plain fetch, with no browser. Every site, page and word is made up; no test
reaches the network, a model or a real browser: `FakeFetcher` is the polite fetcher and `FakeChrome` plays the
rendered page."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser.recipes import html_record
from app.jobsearch.browser.recipes import html_run as html_recipe
from app.jobsearch.browser.recipes import live as recipe_live
from app.jobsearch.browser.recipes import page_run as page_recipe
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch import polite
from app.jobsearch.models import JobPosting
from app.jobsearch.postings import site_reads
from app.sites import html_select as hs
from app.sites import skills, where_view
from app.sites.models import Site, SiteRecipe, SiteRecording
from tests.builders import browser_world
from tests.support import OWNER

NOW = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)
HOST = "careers.example.test"
START = f"https://{HOST}/openings"
LONG = "Five years of programs experience is needed. " * 20  # a whole posting is at least 500 characters


def listing(numbers, *, next_to: str | None = None, place: bool = True, dated: bool = True) -> str:
    rows = "".join(
        f'<li class="row"><a class="job" href="/job/{n}">Programs Director {n}</a>'
        + ('<span class="job-location">Springdale</span>' if place else "")
        + (f'<time class="job-date" datetime="2026-10-{10 + n % 9:02d}">a while ago</time>' if dated else "")
        + "</li>" for n in numbers)
    pager = f'<a class="pager" rel="next" href="{next_to}">Next</a>' if next_to else ""
    return ("<html><body><nav><a href='/about-us'>About the company</a><a href='/login'>Sign in here</a></nav>"
            f"<ul class='results'>{rows}</ul>{pager}</body></html>")


def posting(n: int, body: str = LONG) -> str:
    return (f"<html><body><h1>Programs Director {n}</h1><div class='job-description'><p>{body}</p></div>"
            "<a href='/openings'>All openings</a></body></html>")


def shell() -> str:
    """What a page that draws itself by script gives a plain fetch: a frame and no postings."""
    return "<html><body><div id='app'></div><script>render()</script></body></html>"


class FakeFetcher:
    """The polite fetcher, scripted: `pages` maps an address to its HTML or to (status, HTML)."""

    def __init__(self, pages: dict[str, str | tuple[int, str]]) -> None:
        self.pages, self.got, self.requests, self.deadline = pages, [], 0, None

    async def allows(self, url: str) -> bool:
        return "disallowed" not in url

    async def get(self, url: str, **_kw) -> httpx.Response:
        if "disallowed" in url:
            raise polite.RobotsRefused(f"robots.txt asks not to read {url}")
        self.got.append(url)
        self.requests += 1
        status, body = (200, self.pages.get(url, "")) if isinstance(self.pages.get(url, ""), str) \
            else self.pages[url]
        if url not in self.pages:
            status = 404
        return httpx.Response(status, text=body, headers={"content-type": "text/html"},
                              request=httpx.Request("GET", url))

    async def close(self) -> None:
        pass


class FakeChrome:
    """The app's Chrome over DevTools: a page is whatever `pages` holds for the address loaded, as rendered."""

    def __init__(self, pages: dict[str, str], status: int = 200) -> None:
        self.pages, self.status = pages, status
        self.events: list[dict] | None = None
        self.navigated: list[str] = []
        self.evaluated: list[str] = []

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        expression = (params or {}).get("expression", "")
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Page.navigate":
            self.navigated.append(params["url"])
            assert self.events is not None
            n = len(self.navigated)
            self.events += [
                {"method": "Network.requestWillBeSent", "params": {
                    "requestId": f"d{n}", "type": "Document", "request": {"method": "GET", "url": params["url"]}}},
                {"method": "Network.responseReceived", "params": {
                    "requestId": f"d{n}", "type": "Document", "response": {"status": self.status,
                                                                          "mimeType": "text/html"}}}]
            return {}
        if method != "Runtime.evaluate":
            return {}
        self.evaluated.append(expression[:30])
        if expression == "document.readyState":
            return {"result": {"value": "complete"}}
        if "outerHTML" in expression:
            here = self.navigated[-1]
            return {"result": {"value": json.dumps({"url": here, "html": self.pages.get(here, "")})}}
        return {"result": {"value": "1"}}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(browser_code, "sleep", no_wait)
    monkeypatch.setattr(page_recipe, "SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(recipe_record, "SETTLE_SECONDS", 0.0)


RECIPE = {"kind": "html", "url": START, "links": 'a[href*="/job/"]', "row": "li.row", "location": "span.job-location",
          "date": "time.job-date", "next": "a.pager", "text": "div.job-description"}


def _recipe(**over) -> dict:
    return {**RECIPE, **over}


def _parsed(**over) -> skills.HtmlRecipe:
    got = skills.parse_recipe(_recipe(**over))
    assert isinstance(got, skills.HtmlRecipe)
    return got


# --- the recipe is data ---------------------------------------------------------------------------------------------


def test_an_html_recipe_is_data_with_selectors_and_a_public_start():
    got = _parsed()
    assert (got.kind, got.links, got.next, got.date) == ("html", 'a[href*="/job/"]', "a.pager", "time.job-date")


def test_a_host_recipe_names_its_host_a_pattern_and_the_text_element():
    got = skills.parse_recipe({"kind": "html_posting", "host": "*.example.test", "url_pattern": r"https://.*/job/\d+",
                               "text": "div.job-description"})
    assert isinstance(got, skills.HtmlPostingRecipe) and got.host == "*.example.test"


@pytest.mark.parametrize("over", [
    {"links": "a.job{color:red}"}, {"links": "a:not(.x)"}, {"next": "a + a"}, {"text": "div</script>"},
    {"url": "http://localhost/jobs"}, {"links": ""}, {"row": None, "location": "span.job-location"},
    {"links": "a[href^=javascript:alert(1)"}, {"text": "x" * 300},
])
def test_a_recipe_with_a_selector_outside_the_grammar_or_a_private_start_is_refused(over):
    with pytest.raises(ValueError):
        skills.parse_recipe(_recipe(**over))


@pytest.mark.parametrize("over", [{"host": "http://careers.example.test/"}, {"url_pattern": ""}, {"text": "a:hover"}])
def test_a_host_recipe_that_is_not_a_host_a_pattern_and_a_selector_is_refused(over):
    with pytest.raises(ValueError):
        skills.parse_recipe({"kind": "html_posting", "host": HOST, "url_pattern": r"https://.*/job/\d+",
                             "text": "div.x", **over})


def test_a_site_read_runs_an_html_recipe_and_the_where_to_look_list_calls_it_code():
    assert "html" in site_reads.RUNNABLE and "html" in site_reads.LEARNED and "html" in where_view.CODE_KINDS


# --- reading a list --------------------------------------------------------------------------------------------------


async def test_the_list_is_read_with_each_rows_place_and_date_and_every_page_is_walked():
    fetch = FakeFetcher({START: listing([1, 2], next_to="/openings?page=2"),
                         f"https://{HOST}/openings?page=2": listing([3])})
    got = await skills.run_recipe(fetch, _recipe())
    assert [p["title"] for p in got.postings] == [f"Programs Director {n}" for n in (1, 2, 3)]
    first = got.postings[0]
    assert (first["url"], first["ext_id"], first["location"], first["posted"]) == (
        f"https://{HOST}/job/1", "/job/1", "Springdale", "2026-10-11")
    assert fetch.got == [START, f"https://{HOST}/openings?page=2"] and got.truncated is False


async def test_a_page_that_repeats_ends_the_walk_and_says_so():
    fetch = FakeFetcher({START: listing([1, 2], next_to="/openings?page=2"),
                         f"https://{HOST}/openings?page=2": listing([1, 2], next_to="/openings?page=3"),
                         f"https://{HOST}/openings?page=3": listing([1, 2], next_to="/openings?page=4")})
    got = await html_recipe.run(fetch, _parsed())
    assert len(got.postings) == 2 and got.truncated and "repeated" in (got.note or "")


async def test_a_next_link_off_the_careers_host_is_never_followed():
    fetch = FakeFetcher({START: listing([1, 2, 3], next_to="https://elsewhere.example.org/openings?page=2")})
    got = await html_recipe.run(fetch, _parsed())
    assert len(got.postings) == 3 and fetch.got == [START]


async def test_the_walk_stops_when_the_nights_time_runs_out_and_the_listing_closes_nothing():
    fetch = FakeFetcher({START: listing([1, 2], next_to="/openings?page=2"),
                         f"https://{HOST}/openings?page=2": listing([3])})
    fetch.deadline = 0.0  # monotonic time is past it
    got = await html_recipe.run(fetch, _parsed())
    assert fetch.got == [START] and got.truncated and "time" in (got.note or "")


async def test_a_link_off_the_site_or_to_a_sign_in_or_an_application_is_not_a_posting():
    page = listing([1]).replace("</ul>", (
        '<li class="row"><a class="job" href="https://other.example.org/job/9">Elsewhere role</a></li>'
        '<li class="row"><a class="job" href="/job/1/apply">Apply now to this</a></li>'
        '<li class="row"><a class="job" href="/job/login">Sign in to continue</a></li></ul>'))
    got = await html_recipe.run(FakeFetcher({START: page}), _parsed())
    assert [p["url"] for p in got.postings] == [f"https://{HOST}/job/1"]


async def test_a_row_with_no_place_or_date_still_reads_and_a_selector_that_is_not_there_is_none():
    got = await html_recipe.run(FakeFetcher({START: listing([1, 2], place=False, dated=False)}), _parsed())
    assert [(p["location"], p["posted"]) for p in got.postings] == [(None, None), (None, None)]


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_a_refusal_is_a_stop_that_cools_the_site(status):
    with pytest.raises(skills.SkillError) as stop:
        await html_recipe.run(FakeFetcher({START: (status, "no")}), _parsed())
    assert stop.value.status == status


async def test_a_bot_check_is_a_stop_that_cools_the_site_never_a_page():
    with pytest.raises(skills.SkillError) as stop:
        await html_recipe.run(FakeFetcher({START: (202, "")}), _parsed())
    assert stop.value.status == 403
    with pytest.raises(skills.SkillError) as stop:
        check = "<html><body>Verify you are human. CAPTCHA</body></html>"
        await html_recipe.run(FakeFetcher({START: check}), _parsed())
    assert stop.value.status == 403


async def test_a_start_that_robots_txt_disallows_is_never_read():
    fetch = FakeFetcher({})
    with pytest.raises(polite.Refused):
        await html_recipe.run(fetch, _parsed(url=f"https://{HOST}/disallowed"))
    assert fetch.got == []


async def test_a_page_with_none_of_its_postings_in_the_html_reads_no_postings():
    got = await html_recipe.run(FakeFetcher({START: shell()}), _parsed())
    assert got.postings == []


# --- a posting's page -----------------------------------------------------------------------------------------------


async def test_a_postings_text_is_the_recipes_text_element_with_the_page_kept_as_the_raw_copy():
    fetch = FakeFetcher({f"https://{HOST}/job/1": posting(1)})
    got = await html_recipe.read_text(fetch, _parsed(), f"https://{HOST}/job/1")
    assert got is not None and got.text and got.text.startswith("Five years") and b"Programs Director 1" in got.raw


async def test_a_page_off_the_careers_host_or_with_no_text_element_is_not_read():
    fetch = FakeFetcher({"https://other.example.org/job/1": posting(1), f"https://{HOST}/job/1": posting(1)})
    assert await html_recipe.read_text(fetch, _parsed(), "https://other.example.org/job/1") is None
    assert await html_recipe.read_text(fetch, _parsed(text=None), f"https://{HOST}/job/1") is None
    assert fetch.got == []


async def test_a_gone_posting_says_so_and_a_page_without_the_element_has_no_text():
    fetch = FakeFetcher({f"https://{HOST}/job/2": (404, "gone"), f"https://{HOST}/job/3": shell()})
    gone = await html_recipe.read_text(fetch, _parsed(), f"https://{HOST}/job/2")
    assert gone is not None and gone.status == 404 and gone.text is None
    empty = await html_recipe.read_text(fetch, _parsed(), f"https://{HOST}/job/3")
    assert empty is not None and empty.text is None


async def test_a_host_recipe_reads_only_the_addresses_its_pattern_matches_whole():
    recipe = skills.parse_recipe({"kind": "html_posting", "host": HOST, "url_pattern": r"https://[^/]+/job/\d+",
                                  "text": "div.job-description"})
    assert isinstance(recipe, skills.HtmlPostingRecipe)
    fetch = FakeFetcher({f"https://{HOST}/job/5": posting(5), f"https://{HOST}/about": posting(6)})
    assert (await html_recipe.read_text(fetch, recipe, f"https://{HOST}/job/5")).text
    assert await html_recipe.read_text(fetch, recipe, f"https://{HOST}/about") is None
    assert fetch.got == [f"https://{HOST}/job/5"]


# --- what the rendered page makes of a recipe ------------------------------------------------------------------------


def test_the_rendered_start_page_makes_the_links_row_place_date_and_next_selectors():
    cands = html_record.list_candidates(hs.parse(listing([1, 2, 3, 4], next_to="/openings?page=2")), START)
    recipe = cands[0]["recipe"]
    assert (recipe["links"], recipe["row"], recipe["location"], recipe["date"], recipe["next"]) == (
        'a[href*="/job/"]', "li.row", "span.job-location", "time.job-date", 'a[rel="next"]')
    assert cands[0]["rendered"] == 4 and len(cands[0]["sample"]) == page_recipe.SAMPLES


def test_a_page_with_no_group_of_posting_links_makes_no_recipe():
    page = "<html><body><a href='/about-us'>About the company</a><a href='/team'>Meet the team</a></body></html>"
    assert html_record.list_candidates(hs.parse(page), START) == []


def test_the_text_element_is_the_first_usual_place_that_holds_a_whole_description():
    assert html_record.text_selector(hs.parse(posting(1))) == '[class*="job-description" i]'
    assert html_record.text_selector(hs.parse(posting(1, body="Too short."))) is None
    article = f"<html><body><article>{LONG}</article></body></html>"
    assert html_record.text_selector(hs.parse(article)) == "article"


def test_a_selector_is_one_whose_first_match_is_the_element_even_when_the_page_repeats_it():
    root = hs.parse("<div><a href='/x' class='n'>Next</a></div><div><a href='/y' class='n'>Next</a></div>")
    node = hs.select(root, "a")[1]
    got = html_record.selector_for(root, node)
    assert got and hs.select_one(root, got) is node


def test_the_pattern_of_a_posting_address_matches_its_siblings_and_not_the_rest_of_the_site():
    pattern = html_record.pattern_for(f"https://{HOST}/job/12345/senior-analyst?src=x", HOST)
    rx = skills.compile_pattern(pattern, "url_pattern")
    assert rx.fullmatch(f"https://{HOST}/job/777/other-role") and not rx.fullmatch(f"https://{HOST}/about")
    assert not rx.fullmatch("https://other.example.org/job/777/other-role")


def test_the_pattern_of_a_deep_posting_address_with_ids_in_its_path_and_query_is_one_a_recipe_may_hold():
    sample = f"https://{HOST}/AB1234CD/JobBoard/5966f8ea-2f33-46ea-aa96-f4f549ba84eb/OpportunityDetail?opportunityId=1b2c"
    rx = skills.compile_pattern(html_record.pattern_for(sample, HOST), "url_pattern")
    assert rx.fullmatch(sample.replace("1b2c", "9d8e"))
    assert not rx.fullmatch(f"https://{HOST}/about")


# --- recording --------------------------------------------------------------------------------------------------------


def chrome(monkeypatch, pages: dict[str, str], status: int = 200) -> FakeChrome:
    fake = FakeChrome(pages, status)
    monkeypatch.setattr(page_recipe, "make_tools", lambda: fake)
    return fake


def plain(monkeypatch, pages: dict) -> FakeFetcher:
    fake = FakeFetcher(pages)
    monkeypatch.setattr(polite, "make", lambda: fake)
    return fake


RENDERED = {START: listing([1, 2, 3, 4, 5], next_to="/openings?page=2"),
            **{f"https://{HOST}/job/{n}": posting(n) for n in range(1, 6)}}


async def _asked(reason: str = "first") -> Site:
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url=START, kind="employer", read_by="app", created_at=NOW - timedelta(days=1))
        session.add(site)
        await session.flush()
        await recipe_record.request(session, OWNER, site, reason, NOW)
        await session.commit()
        return site


async def _host_asked():
    async with db.session_factory()() as session:
        rec = SiteRecording(owner=OWNER, site_id=None, host=HOST, reason="first", state="requested",
                            requested_at=NOW, url=f"https://{HOST}/job/1")
        session.add(rec)
        await session.commit()
        return rec.id


async def _rec_id(site: Site):
    async with db.session_factory()() as session:
        return (await session.scalar(select(SiteRecording).where(SiteRecording.site_id == site.id))).id


async def _only(model):
    async with db.session_factory()() as session:
        return list((await session.scalars(select(model))).all())


async def test_a_page_the_plain_fetch_gets_the_same_is_recorded_verified_on_it_and_kept_live(js, monkeypatch):
    site = await _asked()
    fake = chrome(monkeypatch, RENDERED)
    fetch = plain(monkeypatch, RENDERED)
    got = await html_record.record_by_code(OWNER, await _rec_id(site))
    assert got["result"] == bp.FINISHED and "5 postings" in got["note"]
    (live,) = await _only(SiteRecipe)
    (rec,) = await _only(SiteRecording)
    assert live.state == "live" and live.site_id == site.id and rec.state == "written"
    kept = live.recipe
    assert (kept["kind"], kept["links"], kept["row"], kept["location"], kept["date"], kept["text"]) == (
        "html", 'a[href*="/job/"]', "li.row", "span.job-location", "time.job-date", '[class*="job-description" i]')
    assert kept["provenance"]["tested_on"] == 5 and kept["provenance"]["model"] is None
    assert START in fake.navigated and START in fetch.got, "recorded in Chrome and verified by plain fetch"
    assert not any("click" in e or "type" in e for e in fake.evaluated), "nothing is typed or clicked"


async def test_a_page_that_needs_javascript_is_not_kept_and_the_recording_says_so(js, monkeypatch):
    site = await _asked()
    chrome(monkeypatch, RENDERED)
    plain(monkeypatch, {START: shell()})
    got = await html_record.record_by_code(OWNER, await _rec_id(site))
    assert got["result"] == bp.FINISHED and got["note"].startswith(html_record.NEEDS_JS)
    assert await _only(SiteRecipe) == []
    (rec,) = await _only(SiteRecording)
    assert rec.state == "failed" and rec.note.startswith("needs JavaScript:") and "0 postings" in rec.note


async def test_a_page_that_server_draws_only_a_few_rows_of_many_is_a_javascript_page(js, monkeypatch):
    site = await _asked()
    rendered = {**RENDERED, START: listing(range(1, 21))}
    chrome(monkeypatch, rendered)
    plain(monkeypatch, {START: listing([1, 2, 3, 4])})  # 4 of 20 rows: under half of what the browser showed
    got = await html_record.record_by_code(OWNER, await _rec_id(site))
    assert got["note"].startswith(html_record.NEEDS_JS) and await _only(SiteRecipe) == []


async def test_a_list_that_reads_by_plain_fetch_but_whose_text_does_not_is_kept_without_the_text(js, monkeypatch):
    site = await _asked()
    chrome(monkeypatch, RENDERED)
    plain(monkeypatch, {START: RENDERED[START], **{f"https://{HOST}/job/{n}": shell() for n in range(1, 6)}})
    got = await html_record.record_by_code(OWNER, await _rec_id(site))
    assert got["result"] == bp.FINISHED and "text is left out" in got["note"]
    (live,) = await _only(SiteRecipe)
    assert live.recipe["text"] is None and live.recipe["links"] == 'a[href*="/job/"]'


@pytest.mark.parametrize("status", [401, 403, 429, 202])
async def test_a_refusal_or_a_bot_check_on_the_plain_fetch_is_a_block_and_the_site_rests(js, monkeypatch, status):
    site = await _asked()
    chrome(monkeypatch, RENDERED)
    plain(monkeypatch, {START: (status, "")})
    got = await html_record.record_by_code(OWNER, await _rec_id(site))
    assert got["result"] == bp.BLOCKED
    (rec,) = await _only(SiteRecording)
    assert rec.state == "requested" and await _only(SiteRecipe) == []


async def test_a_refusal_in_chrome_is_a_block_and_a_robots_disallow_is_never_loaded(js, monkeypatch):
    site = await _asked()
    chrome(monkeypatch, RENDERED, status=403)
    plain(monkeypatch, RENDERED)
    assert (await html_record.record_by_code(OWNER, await _rec_id(site)))["result"] == bp.BLOCKED
    async with db.session_factory()() as session:
        other = Site(owner=OWNER, url=f"https://{HOST}/disallowed", kind="employer", read_by="app", created_at=NOW)
        session.add(other)
        await session.flush()
        await recipe_record.request(session, OWNER, other, "first", NOW)
        await session.commit()
    fake = chrome(monkeypatch, RENDERED)
    got = await html_record.record_by_code(OWNER, await _rec_id(other))
    assert got["result"] == bp.FINISHED and "robots.txt" in got["note"] and fake.navigated == []


async def test_a_new_recording_retires_the_version_before_it(js, monkeypatch):
    site = await _asked()
    async with db.session_factory()() as session:
        session.add(SiteRecipe(owner=OWNER, site_id=site.id, version=1, recipe=_recipe(), state="live", created_at=NOW))
        await session.commit()
    chrome(monkeypatch, RENDERED)
    plain(monkeypatch, RENDERED)
    await html_record.record_by_code(OWNER, await _rec_id(site))
    versions = sorted((r.version, r.state) for r in await _only(SiteRecipe))
    assert versions == [(1, "retired"), (2, "live")]


async def test_a_host_posting_page_is_recorded_verified_by_plain_fetch_and_kept_as_the_hosts_recipe(js, monkeypatch):
    rec_id = await _host_asked()
    chrome(monkeypatch, RENDERED)
    plain(monkeypatch, RENDERED)
    got = await html_record.record_by_code(OWNER, rec_id)
    assert got["result"] == bp.FINISHED
    (live,) = await _only(SiteRecipe)
    assert live.site_id is None and live.host == HOST and live.recipe["kind"] == "html_posting"
    recipe = skills.parse_recipe(live.recipe)
    assert isinstance(recipe, skills.HtmlPostingRecipe) and html_recipe.covers(recipe, f"https://{HOST}/job/9")


async def test_a_host_posting_page_whose_text_needs_javascript_keeps_no_recipe(js, monkeypatch):
    rec_id = await _host_asked()
    chrome(monkeypatch, RENDERED)
    plain(monkeypatch, {f"https://{HOST}/job/1": shell()})
    got = await html_record.record_by_code(OWNER, rec_id)
    assert got["note"].startswith(html_record.NEEDS_JS) and await _only(SiteRecipe) == []
    (rec,) = await _only(SiteRecording)
    assert rec.state == "failed"


# --- where the data recording hands over ------------------------------------------------------------------------


def _json_response() -> dict:
    return {"method": "GET", "url": f"https://{HOST}/api/jobs", "post_data": None, "status": 200,
            "content_type": "application/json", "body": json.dumps({"jobs": [{"title": "x", "id": 1}] * 3})}


async def _handed(js, monkeypatch, raw, *, host_page: bool) -> list:
    called: list = []

    async def capture(url, tools=None, interact=True, keyword=None):
        return raw, None

    async def html(owner, rec_id):
        called.append(rec_id)
        return {"result": bp.FINISHED, "note": "html"}

    monkeypatch.setattr(recipe_record, "capture_page", capture)
    monkeypatch.setattr(html_record, "record_by_code", html)
    if host_page:
        rec_id = await _host_asked()
        async with db.session_factory()() as session:
            rec = await session.get(SiteRecording, rec_id)
            host = rec.host
    else:
        await _asked()
        host = HOST
    await recipe_record.record_by_code(OWNER, host)
    return called


async def test_a_listing_that_made_no_data_request_goes_on_to_the_page_s_own_html(js, monkeypatch):
    assert len(await _handed(js, monkeypatch, [], host_page=False)) == 1


async def test_a_posting_page_that_made_no_data_request_goes_on_to_the_page_s_own_html(js, monkeypatch):
    assert len(await _handed(js, monkeypatch, [], host_page=True)) == 1


async def test_a_page_with_data_to_repeat_stays_with_the_data_recording(js, monkeypatch):
    assert await _handed(js, monkeypatch, [_json_response()], host_page=False) == []
    (rec,) = await _only(SiteRecording)
    assert rec.state == "recorded"


async def test_a_request_the_data_writer_found_no_recipe_for_goes_on_to_the_pages_own_html(js, monkeypatch):
    site = await _asked()
    rec_id = await _rec_id(site)
    async with db.session_factory()() as session:
        rec = await session.get(SiteRecording, rec_id)
        rec.body, rec.note = b"x", "no data recipe can be made: only cookie and locale requests"
        await session.commit()
    called: list = []

    async def html(owner, rid):
        called.append(rid)
        return {"result": bp.FINISHED, "note": "html"}

    async def capture(*a, **k):
        raise AssertionError("the page is not captured again")

    monkeypatch.setattr(recipe_record, "capture_page", capture)
    monkeypatch.setattr(html_record, "record_by_code", html)
    await recipe_record.record_by_code(OWNER, HOST)
    assert called == [rec_id]


async def test_an_html_recording_after_a_none_keeps_both_reasons_when_it_fails(js, monkeypatch):
    site = await _asked()
    rec_id = await _rec_id(site)
    async with db.session_factory()() as session:
        rec = await session.get(SiteRecording, rec_id)
        rec.body, rec.note = b"x", "no data recipe can be made: only analytics"
        await session.commit()
    chrome(monkeypatch, {START: "<html><body><p>nothing here</p></body></html>"})
    plain(monkeypatch, {})
    await html_record.record_by_code(OWNER, rec_id)
    (rec,) = await _only(SiteRecording)
    assert rec.state == "failed" and "only analytics" in rec.note


# --- reading what the recording kept ----------------------------------------------------------------------------


async def _live_site() -> SiteRecipe:
    async with db.session_factory()() as session:
        site = Site(owner=OWNER, url=START, kind="employer", read_by="app", created_at=NOW - timedelta(days=1))
        session.add(site)
        await session.flush()
        row = SiteRecipe(owner=OWNER, site_id=site.id, version=1, recipe=_recipe(), state="live", created_at=NOW)
        session.add(row)
        await session.commit()
        return row


async def test_the_nightly_read_runs_a_live_html_recipe_through_the_fetcher_and_puts_its_postings_in_the_index(js):
    await browser_world(extra_sites=False)
    await _live_site()
    fetch = FakeFetcher({START: listing([1, 2])})
    async with db.session_factory()() as session:
        _built_in, reads = await site_reads.plan(session, OWNER, [])
        assert [site_reads.kind_of(r.skill) for r in reads] == ["html"]
        counts = await site_reads.refresh(session, OWNER, fetch, reads, NOW)
        await session.commit()
    assert counts["new"] == 2 and counts["failed"] == 0 and fetch.got == [START]
    async with db.session_factory()() as session:
        titles = sorted(p.title for p in (await session.scalars(select(JobPosting))).all())
    assert titles == ["Programs Director 1", "Programs Director 2"]


async def test_two_nights_that_read_nothing_make_an_html_recipe_stale_and_ask_a_re_record(js):
    await browser_world(extra_sites=False)
    live = await _live_site()
    fetch = FakeFetcher({START: shell()})
    for night in (0, 1):
        async with db.session_factory()() as session:
            _built_in, reads = await site_reads.plan(session, OWNER, [])
            await site_reads.refresh(session, OWNER, fetch, reads, NOW + timedelta(days=night))
            await session.commit()
    async with db.session_factory()() as session:
        assert (await session.get(SiteRecipe, live.id)).state == "stale"
        asked = (await session.scalars(select(SiteRecording))).all()
    assert [(r.reason, r.state) for r in asked] == [("repair", "requested")]


async def test_a_read_item_on_an_html_recipes_site_is_read_by_selector_with_no_browser(js, monkeypatch):
    await _live_site()
    fetch = FakeFetcher({f"https://{HOST}/job/7": posting(7)})
    monkeypatch.setattr(polite, "make", lambda: fetch)
    async with db.session_factory()() as session:
        found = await recipe_live.page_recipe_for(session, OWNER, f"https://{HOST}/job/7")
        assert found is not None and found.recipe["kind"] == "html"
        assert await recipe_live.page_recipe_for(session, OWNER, "https://other.example.org/job/7") is None
    job = browser_code.Job(item_id="i", kind=bp.READ, link=f"https://{HOST}/job/7", domain="example.test", title=None,
                           named=False)
    job.page_recipe = found.recipe

    async def turn():
        return None

    got = await browser_code.read_by_code(job, frozenset(), turn)
    assert got is not None and got["result"] == "text" and got["text"].startswith("Five years")
    assert b"Programs Director 7" in got["raw"]


async def test_a_read_item_on_a_hosts_html_posting_recipe_is_read_by_selector_and_a_gone_page_is_gone(js, monkeypatch):
    recipe = {"kind": "html_posting", "host": HOST, "url_pattern": r"https://[^/]+/job/\d+",
              "text": "div.job-description"}
    fetch = FakeFetcher({f"https://{HOST}/job/7": posting(7), f"https://{HOST}/job/8": (404, "gone")})
    monkeypatch.setattr(polite, "make", lambda: fetch)

    async def turn():
        return None

    def job(n: int) -> browser_code.Job:
        item = browser_code.Job(item_id="i", kind=bp.READ, link=f"https://{HOST}/job/{n}", domain="example.test",
                                title=None, named=False)
        item.recipe, item.recipe_id = recipe, "r"
        return item

    got = await browser_code.read_by_code(job(7), frozenset(), turn)
    assert got is not None and got["result"] == "text" and got["via_recipe"] is True
    gone = await browser_code.read_by_code(job(8), frozenset(), turn)
    assert gone is not None and gone["result"] == bp.GONE
