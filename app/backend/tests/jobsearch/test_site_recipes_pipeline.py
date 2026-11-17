"""Site recipes, read live: the `record` browser item that
keeps a site's listing traffic, the parse job that has a session write a recipe from it, and the nightly live read
that writes raw and cleansed copies beside the reads in force, keeps the site's health and asks for a re-record when a
recipe breaks. Every site, page and answer is made up; no test reaches the network or a browser."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch import actions
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser import queue as bq
from app.jobsearch.browser import reader as br
from app.jobsearch.browser import takeable
from app.jobsearch.browser.recipes import live as recipe_live
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.browser.takeable import Taker
from app.jobsearch.doors import scout as mcp_server
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.jobs import parse as role_parse
from app.jobsearch.models import BrowserItem, JobPosting, ParseJob, RawPosting
from app.jobsearch.postings import site_reads
from app.jobsearch.work import session_kinds
from app.settings import settings
from app.sites import recipes
from app.sites.models import Site, SiteRecipe, SiteRecording
from tests.builders import MINUTE, browser_world, parse_runner
from tests.support import DEFAULT_OWNER, OWNER, TOKEN

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out")

NOW = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
CODE = "code-test0002"
HOST = "careers.example.test"
PAGE = f"https://{HOST}/jobs"
API = f"https://{HOST}/api/jobs"


def _rows(start: int, n: int) -> list[dict]:
    return [{"reqId": f"R{i}", "name": f"Role {i}", "link": f"/job/R{i}", "city": ["Remote", "Ohio"],
             "opened": f"2026-10-{(i % 27) + 1:02d}"} for i in range(start, start + n)]


def _raw(**over) -> dict:
    base = {"method": "GET", "url": f"{API}?offset=0&limit=3", "post_data": None, "status": 200,
            "content_type": "application/json", "body": json.dumps({"results": _rows(0, 3)})}
    return {**base, **over}


def _recipe(**over) -> dict:
    base = {"kind": "request", "method": "GET", "url": API, "items": "results",
            "params": {"offset": "{offset}", "limit": "{size}"}, "page": {"param": "offset", "size": 3},
            "fields": {"title": "name", "url": "link", "ext_id": "reqId", "location": "city", "posted": "opened"},
            "transforms": {"location": [{"op": "join", "sep": " / "}]}, "url_prefix": f"https://{HOST}"}
    return {**base, **over}


async def _site(owner: str, url: str = PAGE) -> Site:
    async with db.session_factory()() as session:
        site = Site(owner=owner, url=url, kind="employer", read_by="app", created_at=NOW - timedelta(days=1))
        session.add(site)
        await session.commit()
        return site


# --- recording ----------------------------------------------------------------------------------------------------


class FakeTools:
    """The app's browser over DevTools, scripted: loading the page makes the page's own requests."""

    def __init__(self, document_status: int = 200, data: list[dict] | None = None,
                 html: str = "<html></html>") -> None:
        self.html = html
        self.events: list[dict] | None = None
        self.calls: list[str] = []
        self.document_status = document_status
        self.data = data if data is not None else [{"url": f"{API}?offset=0&limit=3", "body": json.dumps(
            {"results": _rows(0, 3)})}]
        self.bodies: dict[str, str] = {}

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def _event(self, name: str, **params) -> None:
        assert self.events is not None
        self.events.append({"method": name, "params": params})

    async def call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        self.calls.append(method)
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Runtime.evaluate" and (params or {}).get("expression") == "document.readyState":
            return {"result": {"value": "complete"}}
        if method == "Runtime.evaluate" and "outerHTML" in (params or {}).get("expression", ""):
            return {"result": {"value": json.dumps({"url": PAGE, "html": self.html})}}
        if method == "Page.navigate":
            self._event("Network.requestWillBeSent", requestId="d", type="Document",
                        request={"method": "GET", "url": params["url"], "headers": {"Cookie": "secret=1"}})
            self._event("Network.responseReceived", requestId="d", type="Document",
                        response={"status": self.document_status, "mimeType": "text/html"})
            self._event("Network.loadingFinished", requestId="d")
            for n, item in enumerate(self.data):
                rid = f"x{n}"
                self.bodies[rid] = item["body"]
                self._event("Network.requestWillBeSent", requestId=rid, type=item.get("type", "XHR"),
                            request={"method": item.get("method", "GET"), "url": item["url"],
                                     "headers": {"Authorization": "Bearer abc"}})
                self._event("Network.responseReceived", requestId=rid, type=item.get("type", "XHR"),
                            response={"status": 200, "mimeType": item.get("mime", "application/json")})
                self._event("Network.loadingFinished", requestId=rid)
            self._event("Network.requestWillBeSent", requestId="img", type="Image",
                        request={"method": "GET", "url": f"https://{HOST}/logo.png"})
            return {}
        if method == "Network.getResponseBody":
            return {"body": self.bodies[params["requestId"]], "base64Encoded": False}
        return {}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(browser_code, "sleep", no_wait)
    monkeypatch.setattr(recipe_record, "SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(recipe_record, "SCROLL_SECONDS", 0.0)


async def test_a_page_load_gives_only_the_json_data_requests_and_never_a_header():
    raw, stop = await recipe_record.capture_page(PAGE, FakeTools())
    assert stop is None and [r["url"] for r in raw] == [f"{API}?offset=0&limit=3"]
    assert set(raw[0]) == {"method", "url", "post_data", "status", "content_type", "body"}
    assert "Bearer" not in json.dumps(raw) and "secret=1" not in json.dumps(raw)


async def test_a_page_that_answers_a_block_is_a_stop_with_nothing_kept():
    raw, stop = await recipe_record.capture_page(PAGE, FakeTools(document_status=403))
    assert (raw, stop) == ([], 403)


async def test_the_page_is_loaded_once_and_scrolled_once_and_nothing_is_clicked_or_typed():
    tools = FakeTools()
    await recipe_record.capture_page(PAGE, tools)
    assert tools.calls.count("Page.navigate") == 1
    assert not {"Input.dispatchMouseEvent", "Input.dispatchKeyEvent", "Input.insertText"} & set(tools.calls)


async def test_a_data_answer_typed_text_or_nothing_is_kept_when_it_is_json_and_a_script_or_non_json_get_is_not():
    guarded = "for (;;);" + json.dumps({"results": _rows(0, 3)})
    tools = FakeTools(data=[
        {"url": f"https://{HOST}/graphql", "method": "POST", "mime": "text/plain",
         "body": json.dumps({"data": {"jobs": _rows(0, 3)}})},
        {"url": f"{API}?guarded=1", "mime": "text/javascript", "body": guarded},
        {"url": f"{API}?empty=1", "mime": "", "body": json.dumps({"results": _rows(3, 3)})},
        {"url": f"{API}?not=1", "mime": "text/plain", "body": "hello"},
        {"url": f"https://{HOST}/api/job-search", "method": "POST", "mime": "text/plain", "body": "not json"},
        {"url": f"https://{HOST}/app.js", "mime": "application/javascript", "type": "Script", "body": "x=1"}])
    raw, stop = await recipe_record.capture_page(PAGE, tools, interact=False)
    assert stop is None
    assert [r["url"] for r in raw] == [f"https://{HOST}/graphql", f"{API}?guarded=1", f"{API}?empty=1",
                                       f"https://{HOST}/api/job-search"]
    assert all(json.loads(r["body"]) for r in raw[:3])  # the guard is taken off
    assert len(recipes.sanitize(raw, PAGE)["responses"]) == 3  # a POST that is not JSON is kept for the note only


async def test_a_page_that_shows_a_bot_check_and_no_data_is_a_stop_with_nothing_kept():
    check = "<html><body>Just a moment... verify you are human <div class=captcha></div></body></html>"
    raw, stop = await recipe_record.capture_page(PAGE, FakeTools(data=[], html=check))
    assert (raw, stop) == ([], recipe_record.BOT_WALL_STATUS)
    raw, stop = await recipe_record.capture_page(PAGE, FakeTools(data=[], html="<html>plain</html>"))
    assert stop is None


async def test_a_record_item_is_made_for_a_requested_recording_taken_by_code_and_kept(js, monkeypatch):
    await browser_world()
    site = await _site(DEFAULT_OWNER)
    async with db.session_factory()() as session:
        await recipe_record.request(session, DEFAULT_OWNER, site)
        await recipe_record.request(session, DEFAULT_OWNER, site)  # asked twice: one request
        await session.commit()
    seen: list[str] = []

    async def fake_capture(url, tools=None, keyword=None):
        seen.append(url)
        return [_raw()], None

    monkeypatch.setattr(recipe_record, "capture_page", fake_capture)
    async with db.session_factory()() as session:
        await bq.refresh(session, DEFAULT_OWNER, NOW)
        await session.commit()
        item = await session.scalar(select(BrowserItem).where(BrowserItem.kind == "record"))
        assert (item.target, item.priority, item.domain, item.link) == (HOST, 0, "example.test", PAGE)
    assert await browser_code.code_once(DEFAULT_OWNER, CODE, NOW) is True
    assert seen == [PAGE]
    async with db.session_factory()() as session:
        item = await session.scalar(select(BrowserItem).where(BrowserItem.kind == "record"))
        rec = await session.scalar(select(SiteRecording))
        job = await session.scalar(select(ParseJob).where(ParseJob.kind == "recipe"))
        count = await session.scalar(select(func.count()).select_from(SiteRecording))
    assert (item.state, count) == ("done", 1) and (rec.state, rec.responses) == ("recorded", 1)
    assert recipes.unpack(rec.body)["responses"][0]["url"].startswith(API) and rec.sha256
    assert job.state == "waiting" and job.fields["recording_id"] == str(rec.id) and "results" in job.text


async def test_no_session_is_ever_given_a_record_item(js):
    await browser_world()
    site = await _site(DEFAULT_OWNER)
    async with db.session_factory()() as session:
        await recipe_record.request(session, DEFAULT_OWNER, site)
        await bq.refresh(session, DEFAULT_OWNER, NOW)
        kinds = tuple(bp.WORK[bp.WORKER] + bp.WORK[bp.PATHFINDER])
        ready = await takeable.ask(session, Taker(DEFAULT_OWNER, bp.WORKER, kinds, owners_only=False), NOW)
    assert all(row.kind != "record" for row, _ in ready)


async def test_a_page_with_no_json_data_request_goes_on_to_the_html_recording_that_ends_it(js, monkeypatch):
    """Old: the request ended failed with "no JSON data request a recipe could repeat". New (docs/PRD.md path 49.1): the
    recording goes on with the page's own HTML (`html_record`), which keeps a recipe or ends the request
    with its why."""
    await browser_world()
    site = await _site(DEFAULT_OWNER)
    async with db.session_factory()() as session:
        await recipe_record.request(session, DEFAULT_OWNER, site)
        await session.commit()

    async def fake_capture(url, tools=None, keyword=None):
        return [_raw(content_type="text/javascript", body="x")], None

    handed: list = []

    async def html_try(owner, rec_id):
        handed.append(rec_id)
        return {"result": bp.FINISHED, "note": "the html try answered"}

    from app.jobsearch.browser.recipes import html_record

    monkeypatch.setattr(recipe_record, "capture_page", fake_capture)
    monkeypatch.setattr(html_record, "record_by_code", html_try)
    got = await recipe_record.record_by_code(DEFAULT_OWNER, HOST)
    assert got["result"] == bp.FINISHED and got["note"] == "the html try answered" and len(handed) == 1
    async with db.session_factory()() as session:
        rec = await session.scalar(select(SiteRecording))
        assert rec.state == "requested" and await session.scalar(select(func.count()).select_from(ParseJob)) == 0


async def test_a_block_cools_the_site_and_asks_for_no_other_way(js, monkeypatch):
    await browser_world()
    site = await _site(DEFAULT_OWNER)
    async with db.session_factory()() as session:
        await recipe_record.request(session, DEFAULT_OWNER, site)
        await session.commit()

    async def blocked(url, tools=None, keyword=None):
        return [], 429

    monkeypatch.setattr(recipe_record, "capture_page", blocked)
    got = await recipe_record.record_by_code(DEFAULT_OWNER, HOST)
    assert got["result"] == bp.BLOCKED and "429" in got["note"]


async def test_a_browser_that_is_not_there_is_a_failed_try_never_a_hand_over_to_a_session(js, monkeypatch):
    await browser_world()
    site = await _site(DEFAULT_OWNER)
    async with db.session_factory()() as session:
        await recipe_record.request(session, DEFAULT_OWNER, site)
        await session.commit()

    async def down(url, tools=None, keyword=None):
        raise browser_code.BrowserUnavailable("not connected")

    monkeypatch.setattr(recipe_record, "capture_page", down)
    await browser_code.code_once(DEFAULT_OWNER, CODE, NOW)
    async with db.session_factory()() as session:
        item = await session.scalar(select(BrowserItem).where(BrowserItem.kind == "record"))
    assert (item.state, item.tries, item.last_result) == ("waiting", 1, "failed") and "not connected" in item.last_note


async def test_a_record_item_that_used_its_tries_ends_the_request_as_failed(js):
    await browser_world()
    site = await _site(DEFAULT_OWNER)
    async with db.session_factory()() as session:
        await recipe_record.request(session, DEFAULT_OWNER, site)
        await recipe_record.give_up(session, DEFAULT_OWNER, HOST, "the browser was never there")
        await session.commit()
        rec = await session.scalar(select(SiteRecording))
        assert (rec.state, rec.note) == ("failed", "the browser was never there")
        assert await recipe_record.wanted(session, DEFAULT_OWNER) == []


# --- writing ------------------------------------------------------------------------------------------------------


@pytest.fixture
async def agent(js, monkeypatch, scout_token):
    monkeypatch.setattr(settings, "owner", OWNER)
    await parse_runner(seen=MINUTE)


@pytest.fixture
def door():
    return SimpleNamespace(headers={"authorization": f"Bearer {TOKEN}"})


async def _recorded(responses: list[dict] | None = None) -> tuple[Site, uuid.UUID]:
    site = await _site(OWNER)
    capture = recipes.sanitize(responses if responses is not None else [_raw()], PAGE, NOW)
    async with db.session_factory()() as session:
        rec = await recipe_record.request(session, OWNER, site)
        rec.state, rec.recorded_at, rec.sha256 = "recorded", NOW, recipes.fingerprint(capture)
        rec.body, rec.responses = recipes.pack(capture), len(capture["responses"])
        await recipe_record.queue_write(session, OWNER, rec, capture)
        await session.commit()
        return site, rec.id


async def _recipes_of(site: Site) -> list[SiteRecipe]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(SiteRecipe).where(SiteRecipe.site_id == site.id)
                                           .order_by(SiteRecipe.version))).all())


async def test_a_session_is_handed_the_recorded_traffic_and_the_shape_to_answer_in(agent, door):
    await _recorded()
    taken = await mcp_server.jobs_parse_next(door)
    assert taken["kind"] == "recipe" and taken["how"] == recipe_record.HOW
    assert "recipe:" in taken["response_template"] and "reqId" in taken["text"]


async def test_the_work_hand_out_says_sonnet_for_a_recipe_and_nothing_for_the_other_parse_jobs(agent):
    await _recorded()
    async with db.session_factory()() as session:
        first = await role_parse.waiting(session, OWNER)
        assert first is not None and role_parse.is_sonnet(first) is True
        job = await session.scalar(select(ParseJob))
        job.kind, job.role_id = "rank", "rank:1"
        await session.flush()
        again = await role_parse.waiting(session, OWNER)
        assert again is not None and role_parse.is_sonnet(again) is False
    assert session_kinds.PARSER == "parser"


async def test_a_recipe_that_reproduces_the_recording_is_kept_live_with_its_provenance(agent, door):
    site, rec_id = await _recorded()
    taken = await mcp_server.jobs_parse_next(door)
    reported = await mcp_server.jobs_parse_report(door, taken["id"], {"recipe": _recipe()})
    assert reported["ok"] is True and reported["state"] == "done"
    [row] = await _recipes_of(site)
    assert (row.version, row.state, row.recording_id) == (1, "live", rec_id)
    prov = row.recipe["provenance"]
    assert prov["recording_sha256"] and prov["tested_on"] == 3
    assert prov["prompt_version"] == recipe_record.PROMPT_VERSION
    async with db.session_factory()() as session:
        assert (await session.get(SiteRecording, rec_id)).state == "written"
        assert (await session.scalar(select(ParseJob))).text == ""


async def test_a_recipe_the_model_signs_with_its_own_provenance_gets_ours(agent, door):
    site, _ = await _recorded()
    taken = await mcp_server.jobs_parse_next(door)
    own = {"recorded_at": "x", "recording_sha256": "fake", "model": "other"}
    await mcp_server.jobs_parse_report(door, taken["id"], {"recipe": _recipe(provenance=own)})
    [row] = await _recipes_of(site)
    assert row.recipe["provenance"]["recording_sha256"] != "fake" and row.recipe["provenance"]["model"] != "other"


async def test_a_recipe_that_does_not_reproduce_the_recording_is_sent_back_then_given_up(agent, door):
    site, rec_id = await _recorded()
    taken = await mcp_server.jobs_parse_next(door)
    bad = {"recipe": _recipe(items="data.jobs")}
    for _ in range(2):
        async with db.session_factory()() as session:
            with pytest.raises(role_parse.ParseError) as caught:
                await role_parse.report(session, OWNER, taken["id"], bad["recipe"] and {"recipe": bad["recipe"]})
            await session.commit()
        assert "changed shape" in str(caught.value) or "no list" in str(caught.value)
    reported = await mcp_server.jobs_parse_report(door, taken["id"], bad)
    assert reported["state"] == "failed"
    assert await _recipes_of(site) == []
    async with db.session_factory()() as session:
        rec = await session.get(SiteRecording, rec_id)
        assert rec.state == "failed" and "reproduced" in rec.note


async def test_a_session_that_says_no_data_recipe_can_be_made_opens_the_pages_own_html_next(agent, door):
    # Edited 2026-10-27 (PRD 49.1): the answer "none" used to end the recording as failed; it now opens the request
    # again, its capture kept, so the next record item tries the page's own HTML.
    site, rec_id = await _recorded()
    taken = await mcp_server.jobs_parse_next(door)
    await mcp_server.jobs_parse_report(door, taken["id"], {"none": "The only request needs a token a script makes."})
    async with db.session_factory()() as session:
        rec = await session.get(SiteRecording, rec_id)
        wanted = await recipe_record.wanted(session, OWNER)
    assert rec.state == "requested" and rec.body is not None and "token a script makes" in rec.note
    assert await _recipes_of(site) == [] and len(wanted) == 1


async def test_a_rewrite_is_the_next_version_and_retires_the_one_before(agent, door):
    site, _ = await _recorded()
    for n in (1, 2):
        taken = await mcp_server.jobs_parse_next(door)
        await mcp_server.jobs_parse_report(door, taken["id"], {"recipe": _recipe()})
        if n == 1:
            async with db.session_factory()() as session:
                rec = await recipe_record.request(session, OWNER, site, "repair")
                capture = recipes.sanitize([_raw()], PAGE, NOW)
                rec.state, rec.recorded_at, rec.sha256 = "recorded", NOW, recipes.fingerprint(capture)
                rec.body, rec.responses = recipes.pack(capture), 1
                await recipe_record.queue_write(session, OWNER, rec, capture)
                await session.commit()
    first, second = await _recipes_of(site)
    assert (first.state, second.state, second.version) == ("retired", "live", 2) and first.retired_at


# --- the live read ------------------------------------------------------------------------------------------------


class Site404:
    """The made-up careers site: its list answers `rows`, each posting's own answer is a description."""

    def __init__(self, rows: list[dict] | None = None, status: int = 200) -> None:
        self.rows = rows if rows is not None else _rows(0, 3)
        self.status, self.asked = status, []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        self.asked.append(request.url.path)
        if self.status != 200:
            return httpx.Response(self.status)
        if request.url.path == "/api/jobs":
            start = int(request.url.params["offset"])
            return httpx.Response(200, json={"results": self.rows[start:start + 3]})
        if request.url.path.startswith("/api/job/"):
            return httpx.Response(200, json={"body": f"<p>The whole text of {request.url.path[-2:]}.</p>"})
        return httpx.Response(404)

    def fetcher(self) -> Fetcher:
        return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(self.handle)), per_host_seconds=0)


async def _live(detail: bool = True, state: str = "live", read_by: str = "browser") -> tuple[Site, SiteRecipe]:
    site = await _site(OWNER)
    async with db.session_factory()() as session:
        (await session.get(Site, site.id)).read_by = read_by
        await session.commit()
    recipe = _recipe(**({"detail": {"url": f"https://{HOST}/api/job/{{ext_id}}", "description": "body",
                                    "transforms": [{"op": "html_text"}]}} if detail else {}))
    async with db.session_factory()() as session:
        row = SiteRecipe(owner=OWNER, site_id=site.id, version=1, recipe=recipe, state=state, created_at=NOW)
        session.add(row)
        await session.commit()
        return site, row


async def _night(web: Site404, now: datetime = NOW) -> dict:
    """One night's read of the boards (`site_reads`), as the step makes it; returns what it counted."""
    async with db.session_factory()() as session:
        fetch = web.fetcher()
        try:
            _built_in, reads = await site_reads.plan(session, OWNER, [])
            counts = await site_reads.refresh(session, OWNER, fetch, reads, now)
            await recipe_live.maintain(session, OWNER, now)
        finally:
            await fetch.close()
        await session.commit()
        return {**counts, "sites": counts["employers"]}


async def _recipe_row(row_id) -> SiteRecipe:
    async with db.session_factory()() as session:
        return await session.get(SiteRecipe, row_id)


async def _postings() -> list[JobPosting]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(JobPosting).order_by(JobPosting.ext_id))).all())


async def test_a_live_recipe_reads_its_site_into_the_index_with_the_detail_text_though_the_browser_had_the_site(js):
    site, row = await _live()
    async with db.session_factory()() as session:
        assert [h for h in await br.browser_sites(session, OWNER)] == [], "the browser no longer reads the site"
    counts = await _night(Site404())
    assert counts["sites"] == 1 and counts["new"] == 3 and counts["failed"] == 0
    found = await _postings()
    assert [p.ext_id for p in found] == ["R0", "R1", "R2"] and all(p.is_open for p in found)
    assert "The whole text of R1." in (found[1].text or "")
    async with db.session_factory()() as session:
        raws = (await session.scalars(select(RawPosting))).all()
    assert {r.reader for r in raws} == {f"recipe:request:{row.id}"}


async def test_a_second_night_asks_for_no_detail_it_already_holds(js):
    await _live()
    web = Site404()
    await _night(web)
    first = len(web.asked)
    await _night(web, NOW + timedelta(days=1))
    assert len(web.asked) - first == 2, "only the list is asked for again"


async def test_a_stale_recipe_gives_its_site_back_to_the_browser_until_it_is_rewritten(js):
    await _live(detail=False, state="stale")
    async with db.session_factory()() as session:
        assert len(await br.browser_sites(session, OWNER)) == 1


async def test_a_night_that_reads_nothing_is_a_failure_and_two_in_a_row_make_the_recipe_stale_and_ask_a_rewrite(js):
    site, row = await _live(detail=False)
    await _night(Site404())
    web = Site404(rows=[])
    assert (await _night(web, NOW + timedelta(days=1)))["failed"] == 1
    assert (await _recipe_row(row.id)).state == "live"
    await _night(web, NOW + timedelta(days=2))
    now = await _recipe_row(row.id)
    assert (now.state, now.bad_nights) == ("stale", 2)
    async with db.session_factory()() as session:
        asked = (await session.scalars(select(SiteRecording).where(SiteRecording.site_id == site.id))).all()
        assert len(await br.browser_sites(session, OWNER)) == 1, "the browser covers meanwhile"
    assert [(r.reason, r.state) for r in asked] == [("repair", "requested")]
    before = len(web.asked)
    await _night(web, NOW + timedelta(days=3))
    assert len(web.asked) == before, "a stale recipe is not read meanwhile"
    assert len(await _postings()) == 3, "and the postings it found are closed by no empty read"


async def test_a_stale_recipe_is_asked_a_rewrite_once_a_week_and_never_twice_while_one_waits(js):
    await _live(detail=False, state="stale")
    web = Site404()
    await _night(web, NOW)
    await _night(web, NOW + timedelta(days=8))
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(SiteRecording)) == 1


async def test_a_fall_far_under_the_recent_median_is_a_bad_night(js):
    _, row = await _live(detail=False)
    full = Site404(rows=_rows(0, 30))
    for day in range(3):
        await _night(full, NOW + timedelta(days=day))
    thin = Site404(rows=_rows(0, 3))
    assert (await _night(thin, NOW + timedelta(days=3)))["failed"] == 1
    assert "median" in (await _recipe_row(row.id)).health[-1]["why"]
    assert len([p for p in await _postings() if p.is_open]) == 30, "a suspect read closes nothing"


async def test_a_block_is_a_stop_that_asks_for_no_rewrite_and_counts_no_bad_night(js):
    site, row = await _live(detail=False)
    web = Site404(status=403)
    for day in range(4):
        counts = await _night(web, NOW + timedelta(days=day))
    now = await _recipe_row(row.id)
    assert (now.state, now.bad_nights, counts["failed"]) == ("live", 0, 0)
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(SiteRecording)) == 0


async def test_after_three_failed_rewrites_the_recipe_needs_the_owners_attention_and_no_more_are_asked(js):
    site, row = await _live(detail=False, state="stale")
    async with db.session_factory()() as session:
        for n in range(3):
            session.add(SiteRecording(owner=OWNER, site_id=site.id, reason="repair", state="failed",
                                      requested_at=NOW + timedelta(hours=n + 1)))
        await session.commit()
    await _night(Site404(), NOW)
    assert (await _recipe_row(row.id)).state == "attention"
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(SiteRecording).where(
            SiteRecording.state == "requested")) == 0


async def test_the_nights_read_goes_on_when_the_upkeep_of_the_stale_recipes_fails(js, monkeypatch):
    async def broken(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(recipe_live, "maintain", broken)
    async with db.session_factory()() as session:
        await actions._recipes_upkeep(session, OWNER)  # returns, never raises


async def test_a_site_that_is_switched_off_is_not_read_by_its_recipe(js):
    site, _ = await _live(detail=False)
    async with db.session_factory()() as session:
        (await session.get(Site, site.id)).on = False
        await session.commit()
    web = Site404()
    assert (await _night(web))["sites"] == 0 and web.asked == []


async def test_a_recipe_request_goes_through_the_polite_fetcher_so_robots_txt_is_honoured(js):
    await _live(detail=False)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /api/")
        raise AssertionError("a disallowed path was fetched")

    async with db.session_factory()() as session:
        fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)
        _built_in, reads = await site_reads.plan(session, OWNER, [])
        counts = await site_reads.refresh(session, OWNER, fetch, reads, NOW)
        await fetch.close()
        await session.commit()
    assert counts["failed"] == 0 and counts["new"] == 0


# --- asking for a recording ---------------------------------------------------------------------------------------


async def _ask_first_recording(ref: str) -> bool:
    """Ask for a first recording of the owner's site that `ref` names (an id, an address or a host); False when no site
    matches, as the owner action `recipe.record` does."""
    async with db.session_factory()() as session:
        site = await recipe_record.find_site(session, OWNER, ref)
        if site is None:
            return False
        await recipe_record.request(session, OWNER, site, "first")
        await session.commit()
        return True


async def test_a_recording_is_asked_for_of_one_of_the_owners_sites_and_an_unknown_host_matches_none(js):
    await _site(OWNER)
    assert await _ask_first_recording("nowhere.example.test") is False
    assert await _ask_first_recording(f"https://{HOST}/anything") is True
    assert await _ask_first_recording(HOST) is True
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(SiteRecording)) == 1


async def test_a_recording_takes_a_site_id_or_an_address_and_prefers_the_switched_on_row_of_a_host(js):
    off = await _site(OWNER, PAGE)
    on = await _site(OWNER, f"https://{HOST}/other")
    async with db.session_factory()() as session:
        (await session.get(Site, off.id)).on = False
        await session.commit()
    assert await _ask_first_recording(HOST)  # a host: the switched-on row
    async with db.session_factory()() as session:
        assert {r.site_id for r in (await session.scalars(select(SiteRecording))).all()} == {on.id}
    assert await _ask_first_recording(PAGE)  # an address names its own row
    assert await _ask_first_recording(str(on.id))  # an id too; asked twice: one request
    async with db.session_factory()() as session:
        assert {r.site_id for r in (await session.scalars(select(SiteRecording))).all()} == {on.id, off.id}
        assert await recipe_record.find_site(session, OWNER, str(uuid.uuid4())) is None
        assert await recipe_record.find_site(session, "someone-else", str(on.id)) is None


async def test_a_sessions_cost_for_writing_a_recipe_is_put_on_the_recording_of_the_site(js):
    _, rec_id = await _recorded()
    async with db.session_factory()() as session:
        job = await session.scalar(select(ParseJob))
        job.fields = {**job.fields, "session_id": "parser-1"}
        await session.commit()
    async with db.session_factory()() as session:
        assert await recipe_record.add_spend(session, OWNER, "other-session", 0.5) == 0
        assert await recipe_record.add_spend(session, OWNER, "parser-1", 0.125) == 1
        await session.commit()
    async with db.session_factory()() as session:
        assert (await session.get(SiteRecording, rec_id)).note == "Writing cost $0.1250 at API prices."


async def test_a_search_requests_whole_body_is_kept_even_when_the_event_holds_only_its_start():
    class Tools(FakeTools):
        async def call(self, method, params=None, session_id=None):
            if method == "Network.getRequestPostData":
                return {"postData": json.dumps({"size": 50, "pad": "x" * 70_000})}
            return await super().call(method, params, session_id)

    tools = Tools(data=[{"url": f"https://{HOST}/api/job-search", "method": "POST",
                         "body": json.dumps({"results": _rows(0, 3)})}])
    raw, _ = await recipe_record.capture_page(PAGE, tools, interact=False)
    assert json.loads(raw[0]["post_data"])["size"] == 50 and len(raw[0]["post_data"]) > 70_000
    assert len(recipes.sanitize(raw, PAGE)["responses"][0]["post_data"]) > 70_000


def test_the_writer_is_shown_a_search_body_far_past_six_hundred_characters():
    body = json.dumps({"filters": ["x" * 40] * 30, "size": 50})
    capture = recipes.sanitize([_raw(method="POST", post_data=body)], PAGE)
    assert 'size\\": 50' in recipes.trim(capture)  # the text is JSON, so its quotes are escaped
