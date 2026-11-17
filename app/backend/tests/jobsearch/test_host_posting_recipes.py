"""A recipe for an employer host's single-posting pages (the owner, 2026-10-22): it needs no site row, is recorded on
one posting's page, written and checked like a site's recipe, and then the browser's read items on that host are read
by code. A page it cannot read goes the way it always went, and two bad days in a row make it stale (the browser
covers) and ask for a re-record. Every host, page and answer is made up; no test reaches the network or a browser."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import pace as bp
from app.jobsearch.browser.recipes import live as recipe_live
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import Opportunity, ParseJob
from app.jobsearch.research import analysis
from app.sites import recipes, skills
from app.sites.models import SiteRecipe, SiteRecording
from tests.builders import browser_world, items
from tests.support import DEFAULT_OWNER as OWNER
from tests.support import POSTING_WORDS

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out")

NOW = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)
CODE = "code-test0009"
HOST = "jobs.fabrikam-careers.example.test"
PAGE = f"https://{HOST}/job/R-{{n}}"
API = f"https://{HOST}/api/posting/R-{{n}}"


def posting_recipe(**over) -> dict:
    base = {"kind": "posting", "host": HOST, "url_pattern": rf"https://{HOST}/job/(?P<ext_id>R-\d+)",
            "detail": {"url": f"https://{HOST}/api/posting/{{ext_id}}", "answer": "json", "description": "body",
                       "transforms": [{"op": "html_text"}]}}
    return {**base, **over}


def _capture(n: int = 1) -> dict:
    raw = [{"method": "GET", "url": API.format(n=n), "post_data": None, "status": 200,
            "content_type": "application/json", "body": json.dumps({"body": "<p>" + POSTING_WORDS + "</p>"})}]
    return recipes.sanitize(raw, PAGE.format(n=n), NOW)


# --- the recipe ---------------------------------------------------------------------------------------------------


def test_a_posting_recipe_names_the_postings_id_and_a_host_may_be_a_family():
    assert skills.parse_recipe(posting_recipe()).kind == "posting"
    assert skills.host_covers("*.family.example.test", "acme.family.example.test")
    assert skills.host_covers("*.family.example.test", "family.example.test")
    assert not skills.host_covers("jobs.example.test", "other.example.test")
    for bad in (posting_recipe(url_pattern=rf"https://{HOST}/job/\d+"), posting_recipe(host=f"https://{HOST}/job")):
        with pytest.raises(ValueError):
            skills.parse_recipe(bad)


async def test_a_posting_recipe_is_checked_against_the_recording_of_the_page(js):
    ok = await recipes.verify_posting_offline(posting_recipe(), _capture(), PAGE.format(n=1))
    assert ok.ok, ok.problems
    wrong = posting_recipe(detail={"url": f"https://{HOST}/api/other/{{ext_id}}", "answer": "json",
                                   "description": "body"})
    bad = await recipes.verify_posting_offline(wrong, _capture(), PAGE.format(n=1))
    assert not bad.ok and "never recorded" in bad.problems[0]
    off_host = await recipes.verify_posting_offline(posting_recipe(host="other.example.test"), _capture(),
                                                    PAGE.format(n=1))
    assert not off_host.ok and "does not cover" in off_host.problems[0]
    thin = _capture()
    thin["responses"][0]["body"] = json.dumps({"body": "Lead the team."})
    assert not (await recipes.verify_posting_offline(posting_recipe(), thin, PAGE.format(n=1))).ok


def _parts_recipe(**detail) -> dict:
    base = posting_recipe()
    parts = [{"path": "data.description"}, {"path": "data.responsibilities", "heading": "Responsibilities"},
             {"path": "data.qualifications", "heading": "Qualifications"}]
    base["detail"] = {"url": f"https://{HOST}/api/posting/{{ext_id}}", "answer": "json",
                      "transforms": [{"op": "html_text"}], "parts": parts, **detail}
    return base


def _parts_capture() -> dict:
    data = {"description": "<p>" + POSTING_WORDS + "</p>",
            "responsibilities": "<ul><li>Run the program office.</li></ul>",
            "qualifications": "<p>Ten years of program delivery.</p>"}
    raw = [{"method": "GET", "url": API.format(n=1), "post_data": None, "status": 200,
            "content_type": "application/json", "body": json.dumps({"data": data})}]
    return recipes.sanitize(raw, PAGE.format(n=1), NOW)


async def test_a_posting_text_split_across_several_fields_is_joined_in_order_with_its_headings(js):
    recipe = skills.parse_recipe(_parts_recipe())
    assert isinstance(recipe, skills.PostingRecipe)
    ok = await recipes.verify_posting_offline(_parts_recipe(), _parts_capture(), PAGE.format(n=1))
    assert ok.ok, ok.problems

    class Replay:
        async def request(self, method, url, **kw):
            return httpx.Response(200, json=json.loads(_parts_capture()["responses"][0]["body"]),
                                  request=httpx.Request(method, url))

    got = await skills.read_posting(Replay(), recipe, PAGE.format(n=1))  # type: ignore[arg-type]
    assert got is not None and got.text is not None
    text = got.text
    assert text.index(POSTING_WORDS.split()[0]) < text.index("Responsibilities") < text.index("Qualifications")
    assert text.index("Run the program office") < text.index("Qualifications\nTen years")


def test_a_detail_names_one_description_path_or_several_parts_never_both_or_neither():
    for bad in ({"description": "data.body"}, {"parts": []}):
        detail = _parts_recipe()["detail"]
        detail.pop("parts")
        detail.update(bad)
        if "description" in bad:
            detail["parts"] = _parts_recipe()["detail"]["parts"]
        with pytest.raises(ValueError):
            skills.parse_recipe({**posting_recipe(), "detail": detail})
    with pytest.raises(ValueError):
        skills.parse_recipe(_parts_recipe(parts=[{"path": "data..a"}]))


# --- recording and writing, with no site row ------------------------------------------------------------------------


async def _role(n: int, url: str, **fields) -> uuid.UUID:
    async with db.session_factory()() as session:
        role = Opportunity(**{
            "owner": OWNER, "slug": f"role-{n}", "company": f"Fabrikam {n}", "title": f"Programs Director {n}",
            "match_key": match_key(f"Fabrikam {n}", f"Programs Director {n}"), "origin": "scout",
            "status": "second_impression", "found": date(2026, 10, 10), "url": url, "sourced_at": NOW,
            "source_url": url, "source_kind": "employer", **fields})
        session.add(role)
        await session.commit()
        return role.id


async def test_a_recording_of_a_host_with_no_site_row_is_asked_for_from_a_role_that_has_a_page_on_it(js):
    async with db.session_factory()() as session:
        assert await recipe_record.request_host(session, OWNER, HOST) is None, "no role has a page on the host"
    await _role(1, PAGE.format(n=1))
    async with db.session_factory()() as session:
        rec = await recipe_record.request_host(session, OWNER, HOST)
        again = await recipe_record.request_host(session, OWNER, HOST)
        await session.commit()
        assert rec is not None and again is not None and rec.id == again.id, "one open request a host"
        assert (rec.site_id, rec.host, rec.url) == (None, HOST, PAGE.format(n=1))
        asks = await recipe_record.wanted(session, OWNER)
    assert asks == [{"site": HOST, "link": PAGE.format(n=1), "domain": "example.test",
                     "priority": 0}]
    async with db.session_factory()() as session:
        family = await recipe_record.request_host(session, OWNER, "*.fabrikam-careers.example.test")
        assert family is not None and family.url == PAGE.format(n=1)


async def test_a_recipe_written_for_a_host_is_kept_live_as_the_hosts_not_a_sites(js):
    await _role(1, PAGE.format(n=1))
    capture = _capture()
    async with db.session_factory()() as session:
        rec = await recipe_record.request_host(session, OWNER, HOST)
        assert rec is not None
        rec.state, rec.recorded_at, rec.sha256 = "recorded", NOW, recipes.fingerprint(capture)
        rec.body, rec.responses = recipes.pack(capture), 1
        job = await recipe_record.queue_write(session, OWNER, rec, capture)
        assert job.fields["posting"] is True and job.fields["host"] == HOST
        outcome, problems = await recipe_record.report(session, OWNER, job, {"recipe": posting_recipe()}, NOW)
        await session.commit()
    assert (outcome, problems) == ("done", [])
    async with db.session_factory()() as session:
        row = await session.scalar(select(SiteRecipe))
    assert (row.site_id, row.host, row.state, row.version) == (None, HOST, "live", 1)
    assert row.recipe["provenance"]["recording_sha256"] and row.recipe["kind"] == "posting"


async def test_the_writer_is_told_to_answer_a_posting_recipe_for_a_postings_page():
    assert "kind posting" in recipe_record.HOW_POSTING and "ext_id" in recipe_record.HOW_POSTING


# --- the browser's read items on the host ---------------------------------------------------------------------------


def serve(monkeypatch, answers: dict[str, tuple[int, object]], robots: str | None = None) -> list[str]:
    """The polite fetch answers each address with (status, JSON); robots.txt is absent unless `robots` is given; any
    other address is a 404."""
    seen: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404) if robots is None else httpx.Response(200, text=robots)
        seen.append(str(request.url))
        status, body = answers.get(str(request.url), (404, "Not found"))
        return httpx.Response(status, json=body) if not isinstance(body, str) else httpx.Response(status, text=body)

    monkeypatch.setattr(polite, "make", lambda: Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0))
    return seen


async def _live_recipe() -> uuid.UUID:
    async with db.session_factory()() as session:
        row = SiteRecipe(owner=OWNER, site_id=None, host=HOST, version=1, recipe=posting_recipe(), state="live",
                         created_at=NOW)
        session.add(row)
        await session.commit()
        return row.id


async def _read_item_for(n: int) -> uuid.UUID:
    return await _role(n, PAGE.format(n=n), analysis_state=analysis.NO_TEXT)


async def test_a_read_on_the_host_is_made_by_the_recipe_and_the_page_itself_is_never_fetched(js, monkeypatch):
    await browser_world(extra_sites=False)
    role_id = await _read_item_for(1)
    await _live_recipe()
    seen = serve(monkeypatch, {API.format(n=1): (200, {"body": "<p>" + POSTING_WORDS + "</p>"})})
    bp.code_running(OWNER, True)
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    assert seen == [API.format(n=1)], "the posting's own data request, and not its page"
    row = (await items(kind="read"))[0]
    assert (row.state, row.last_result) == ("done", "text")
    async with db.session_factory()() as session:
        assert "programs" in ((await session.get(Opportunity, role_id)).description or "").lower()


async def test_a_page_the_recipe_cannot_read_goes_as_it_always_went_and_two_bad_days_make_it_stale(js, monkeypatch):
    await browser_world(extra_sites=False)
    for n in (1, 2, 3):  # a read the code could not make goes to a session, so each day has a read of its own
        await _read_item_for(n)
    recipe_id = await _live_recipe()
    refused = {PAGE.format(n=n): (403, "no") for n in (1, 2, 3)}  # and the page itself is a stop for code
    serve(monkeypatch, {**refused, **{API.format(n=n): (500, "boom") for n in (1, 2, 3)}})
    bp.code_running(OWNER, True)
    for day in range(3):
        await browser_code.code_once(OWNER, CODE, NOW + timedelta(days=day, minutes=day))
    async with db.session_factory()() as session:
        row = await session.get(SiteRecipe, recipe_id)
    assert row.state == "stale" and row.bad_nights == 2
    async with db.session_factory()() as session:
        asked = (await session.scalars(select(SiteRecording).where(SiteRecording.host == HOST))).all()
    assert [(r.reason, r.state) for r in asked] == [("repair", "requested")]
    reads = await items(kind="read")
    assert len(reads) == 3 and all(r.state == "waiting" and r.code_at is not None for r in reads), (
        "the reads the recipe could not make are the browser's, as before")


async def test_a_stale_host_recipe_is_not_used(js, monkeypatch):
    async with db.session_factory()() as session:
        session.add(SiteRecipe(owner=OWNER, site_id=None, host=HOST, version=1, recipe=posting_recipe(),
                               state="stale", created_at=NOW))
        await session.commit()
        assert await recipe_live.posting_recipe_for(session, OWNER, PAGE.format(n=1)) is None


async def test_a_family_recipe_covers_its_hosts_and_the_narrowest_wins(js):
    async with db.session_factory()() as session:
        for version, host in enumerate(("*.fabrikam-careers.example.test", HOST), start=1):
            session.add(SiteRecipe(owner=OWNER, site_id=None, host=host, version=version,
                                   recipe=posting_recipe(host=host), state="live", created_at=NOW))
        await session.commit()
        found = await recipe_live.posting_recipe_for(session, OWNER, PAGE.format(n=1))
        other = await recipe_live.posting_recipe_for(session, OWNER, "https://acme.fabrikam-careers.example.test/job/R-5")
        none = await recipe_live.posting_recipe_for(session, OWNER, "https://elsewhere.example.test/job/R-5")
    assert found is not None and found.host == HOST
    assert other is not None and other.host == "*.fabrikam-careers.example.test" and none is None


# --- a host whose postings sit on `www.` (the recipe's host and the address are one site, `www.` left out) ----------


WWW_HOST = f"www.{HOST}"
WWW_PAGE = f"https://{WWW_HOST}/en/jobs/{{n}}/programs-director"
WWW_API = f"https://{WWW_HOST}/api/jobs/{{n}}"
WWW_ROBOTS = "User-agent: *\nDisallow: /en/search\nDisallow: /api/search\nAllow: /api/jobs\n"


def www_recipe() -> dict:
    return posting_recipe(host=WWW_HOST, url_pattern=rf"https://{WWW_HOST}/en/jobs/(?P<ext_id>\d+)/[^/#]+",
                          detail={"url": WWW_API.replace("{n}", "{ext_id}"), "answer": "json",
                                  "description": "body", "transforms": [{"op": "html_text"}]})


def test_a_hosts_www_is_left_out_on_both_sides_of_the_comparison():
    assert skills.host_covers(WWW_HOST, HOST) and skills.host_covers(HOST, WWW_HOST)
    assert skills.host_covers(WWW_HOST, WWW_HOST) and not skills.host_covers(WWW_HOST, "other.example.test")
    assert skills.host_covers(f"*.{HOST}", f"www.{HOST}") and not skills.host_covers(WWW_HOST, f"jobs.{HOST}")


async def test_a_recording_is_asked_for_a_host_named_with_its_www_when_roles_hold_its_postings(js):
    await _role(1, WWW_PAGE.format(n=1001))
    async with db.session_factory()() as session:
        assert await recipe_record.sample_posting(session, OWNER, WWW_HOST) == WWW_PAGE.format(n=1001)
        assert await recipe_record.sample_posting(session, OWNER, HOST) == WWW_PAGE.format(n=1001)
        rec = await recipe_record.request_host(session, OWNER, WWW_HOST)
        await session.commit()
    assert rec is not None and (rec.host, rec.url) == (WWW_HOST, WWW_PAGE.format(n=1001))


async def test_a_recipe_for_a_www_host_reads_its_postings_by_code_and_honours_robots_txt(js, monkeypatch):
    await browser_world(extra_sites=False)
    role_id = await _role(1, WWW_PAGE.format(n=1001), analysis_state=analysis.NO_TEXT)
    async with db.session_factory()() as session:
        session.add(SiteRecipe(owner=OWNER, site_id=None, host=WWW_HOST, version=1, recipe=www_recipe(),
                               state="live", created_at=NOW))
        await session.commit()
        assert await recipe_live.posting_recipe_for(session, OWNER, WWW_PAGE.format(n=1001)) is not None
    seen = serve(monkeypatch, {WWW_API.format(n=1001): (200, {"body": "<p>" + POSTING_WORDS + "</p>"})},
                 robots=WWW_ROBOTS)
    bp.code_running(OWNER, True)
    assert await browser_code.code_once(OWNER, CODE, NOW) is True
    assert seen == [WWW_API.format(n=1001)], "the posting's own data request, and not its page"
    row = (await items(kind="read"))[0]
    assert (row.state, row.last_result) == ("done", "text")
    async with db.session_factory()() as session:
        assert "programs" in ((await session.get(Opportunity, role_id)).description or "").lower()


async def test_a_data_request_that_robots_txt_disallows_is_never_made_by_the_recipe(js, monkeypatch):
    await browser_world(extra_sites=False)
    await _role(1, WWW_PAGE.format(n=1001), analysis_state=analysis.NO_TEXT)
    async with db.session_factory()() as session:
        session.add(SiteRecipe(owner=OWNER, site_id=None, host=WWW_HOST, version=1, recipe=www_recipe(),
                               state="live", created_at=NOW))
        await session.commit()
    seen = serve(monkeypatch, {WWW_API.format(n=1001): (200, {"body": "<p>" + POSTING_WORDS + "</p>"})},
                 robots="User-agent: *\nDisallow: /api/\n")
    bp.code_running(OWNER, True)
    await browser_code.code_once(OWNER, CODE, NOW)
    assert WWW_API.format(n=1001) not in seen, "a disallowed address is never requested"


# --- asking for a recording ---------------------------------------------------------------------------------------


async def test_a_recording_is_asked_for_each_host_and_a_host_with_no_page_to_record_gets_none(js):
    await _role(1, PAGE.format(n=1))
    async with db.session_factory()() as session:
        asked = {host: await recipe_record.request_host(session, OWNER, host, "first")
                 for host in (HOST, "nowhere.example.test")}
        await session.commit()
    assert asked[HOST] is not None and asked["nowhere.example.test"] is None
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(SiteRecording))).all()) == 1
        assert await session.scalar(select(ParseJob)) is None
