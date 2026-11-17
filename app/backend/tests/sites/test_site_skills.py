"""A site's skill and its Check (docs/PRD.md path 29.2): the recipe format and its runner (`sites/skills.py`), the saved
versions, and the code Check that runs the newest skill and counts (`sites/site_check.py`). Every site is made up and
sits behind a mock transport; nothing touches the network."""

import json
import time
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from app import db
from app.jobsearch import adapters, ats
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher, Refused
from app.sites import registry, skills
from app.sites.models import Project, Site, SiteSkill
from app.sites.site_check import expected_count, site_check
from app.sites.skills import SkillError, current_skill, parse_recipe, run_recipe, save_skill
from tests.sites.builders import LABEL_A, SYSTEM_A, UNREAD, board_of
from tests.support import OWNER

HOST = "https://jobs.example.test"
#: A job system the app reads that takes more than a name to address a board: its own tests of what a part may hold are
#: the system's (the restricted tier keeps those of the systems that do not ship).
MANY_PARTS = next(
    r.key for r in registry.current().runbooks
    if r.kind == "job_system" and r.key in adapters.ADAPTERS and len(ats.SYSTEMS[r.key].needs) > 1
    and registry.verdict(r.hosts[0]) in ("allows", "silent"))


def _fetcher(handle, seen: list[httpx.Request] | None = None) -> tuple[Fetcher, list[httpx.Request]]:
    seen = [] if seen is None else seen

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(request)
        return handle(request)

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0), seen


def _job(n: int, **over) -> dict:
    row = {"id": n, "name": f"Role {n}", "path": f"/jobs/{n}", "loc": {"name": "Remote - US"}, "date": "2026-10-01",
           "blurb": f"<p>Do thing {n}.</p>"}
    return {**row, **over}


def _request(**over) -> dict:
    recipe = {
        "kind": "request", "method": "GET", "url": f"{HOST}/api/jobs", "items": "data.results",
        "fields": {"title": "name", "url": "path", "location": "loc.name", "posted": "date", "summary": "blurb"},
        "url_prefix": HOST,
    }
    return {**recipe, **over}


def _world(rows=None, job_status: int = 200):
    """The made-up site: its data request answers with `rows`, and each posting's own page answers `job_status`."""
    rows = [_job(n) for n in range(1, 5)] if rows is None else rows

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/jobs":
            return httpx.Response(200, json={"data": {"results": rows}})
        if request.url.path.startswith("/jobs/"):
            return httpx.Response(job_status, text="a posting")
        return httpx.Response(404)

    return handle


LIST_ONE = """<html><body><ul>
<li><a href="/careers/jobs/101-analyst">Data Analyst</a></li>
<li><a href="/careers/jobs/102-lead"> Team  <b>Lead</b></a></li>
<li><a href="/careers/jobs/102-lead#apply">Team Lead</a></li>
<li><a href="/about">About us</a></li></ul>
<a href="/careers?p=2">Next</a></body></html>"""
LIST_TWO = """<ul><li>
<a href="https://jobs.example.test/careers/jobs/103-ops" title="Ops Manager"><img src="x.png"></a></li>
</ul><a href="/careers">Previous</a><a href="/careers?p=3">Next</a>"""
LIST_THREE = """<ul><li><a href="/careers/jobs/104-pm">Program Manager</a></li></ul>"""


def _list_page(**over) -> dict:
    recipe = {"kind": "list_page", "url": f"{HOST}/careers", "link_pattern": r"/careers/jobs/\d+",
              "next_pattern": "^next$"}
    return {**recipe, **over}


# --- the recipe is checked --------------------------------------------------------------------------------------------


def test_every_kind_of_recipe_is_accepted():
    assert parse_recipe({"kind": "adapter", "ats": SYSTEM_A, "board": board_of("acme")}).ats == SYSTEM_A
    many = {key: "acme" for key in ats.SYSTEMS[MANY_PARTS].needs}
    assert parse_recipe({"kind": "adapter", "ats": MANY_PARTS, "board": many}).kind == "adapter"
    r = parse_recipe(_request(method="POST", body={"q": "x"}, page={"param": "offset", "size": 20, "in": "body"}))
    assert r.page.where == "body" and r.body == {"q": "x"}
    assert parse_recipe(_list_page(title_from="title_attr")).title_from == "title_attr"


@pytest.mark.parametrize(("raw", "reason"), [
    (SYSTEM_A, "JSON object"),
    ({}, "unknown recipe kind None"),
    ({"kind": "script", "code": "1"}, "unknown recipe kind 'script'"),
    ({"kind": "adapter", "ats": SYSTEM_A, "board": {"slug": "a"}, "run": "x"},
     "run: is not part of the adapter recipe"),
    ({"kind": "adapter", "ats": "nonesuch", "board": {"slug": "a"}}, "not a job system the app knows"),
    ({"kind": "adapter", "ats": UNREAD, "board": board_of("a")}, "has no reader yet"),
    ({"kind": "adapter", "ats": MANY_PARTS, "board": {ats.SYSTEMS[MANY_PARTS].needs[0]: "acme"}},
     "board lacks the parts"),
    ({"kind": "adapter", "ats": SYSTEM_A}, "board: is missing"),
    (_request(fields={"title": "name"}), "fields needs url"),
    (_request(fields={"url": "path"}), "fields needs title"),
    (_request(fields={"title": "name", "url": "path", "salary": "pay"}), "fields.salary is not a posting field"),
    (_request(fields={"title": "name", "url": "a b;c!"}), "fields.url is not a dotted path"),
    (_request(items="a..b"), "items is not a dotted path"),
    (_request(method="PUT"), "method"),
    (_request(body={"q": 1}), "a GET request has no body"),
    (_request(page={"param": "offset", "size": 10, "in": "body"}), "page.in is body"),
    (_request(page={"param": "token", "size": 10}), "page.param"),
    (_request(page={"param": "page", "size": 5000}), "page.size"),
    (_request(url="ftp://jobs.example.test/x"), "public http(s) address"),
    (_request(url="http://localhost:8080/api"), "public http(s) address"),
    (_request(url="http://127.0.0.1/api"), "public http(s) address"),
    (_request(url="http://192.168.1.5/api"), "public http(s) address"),
    (_request(url="http://169.254.169.254/latest"), "public http(s) address"),
    (_request(url="http://intranet/api"), "public http(s) address"),
    (_request(url="http://wiki.internal/api"), "public http(s) address"),
    (_request(url="https://user:secret@jobs.example.test/api"), "public http(s) address"),
    (_request(url_prefix="http://10.0.0.1/"), "url_prefix must be a public"),
    (_list_page(url="file:///etc/passwd"), "public http(s) address"),
    (_list_page(link_pattern="x" * 301), "longer than 300"),
    (_list_page(link_pattern="[unclosed"), "not a valid pattern"),
    (_list_page(title_from="guess"), "title_from"),
    ({"kind": "page_text", "url": f"{HOST}/careers"}, "unknown recipe kind 'page_text'"),
    (_list_page(max_pages=2), "max_pages: is not part of the list_page recipe"),
    (_request(page={"param": "page", "size": 2, "max_pages": 3}), "page.max_pages: is not part of the request recipe"),
])
def test_a_bad_recipe_is_refused_with_its_reason(raw, reason):
    with pytest.raises(ValueError) as caught:
        parse_recipe(raw)
    assert reason in str(caught.value)


#: Shaped like keys (the app's secret check refuses these), built from parts so no scanner takes them for real ones.
A_KEY = "AKIA" + "A" * 16
G_KEY = "ghp_" + "b" * 26


def _search(**over) -> dict:
    return {"how": "boxes", "phrase_box": "the Search jobs box at the top", "place_box": "the City box beside it",
            **over}


def _results(**over) -> dict:
    return {"row": "a card with a bold title and the company under it",
            "fields": {"title": "the bold line", "company": "the line under the title", "posted": "the grey date"},
            "next": "the Next button under the list", **over}


def _browser(**over) -> dict:
    return {"kind": "browser", "start_url": f"{HOST}/jobs", "search": _search(), "results": _results(),
            "posting": {"description": "the long text under About the role"}, **over}


def test_a_browser_recipe_is_accepted_with_all_its_parts_or_only_the_ones_that_are_needed():
    bare = parse_recipe(_browser())
    assert (bare.kind, bare.search.how, bare.results.fields["title"]) == ("browser", "boxes", "the bold line")
    assert bare.recommended_url is None and bare.notes == [] and bare.posting.apply is None
    full = parse_recipe(_browser(
        search=_search(recency={"control": "the Date posted menu", "choices": ["Past 24 hours", "Past week"]},
                       newest="the Sort menu, choose Newest"),
        results=_results(fields={"title": "t", "company": "c", "location": "l", "posted": "p", "pay": "m",
                                 "work_mode": "w", "snippet": "s", "apply_url": "a"}),
        posting={"description": "under About the role", "apply": "the Apply link at the top"},
        recommended_url=f"{HOST}/recommended", notes=["the City box wants a city, not a state"]))
    assert full.search.recency.choices == ["Past 24 hours", "Past week"] and full.posting.apply
    assert len(full.results.fields) == 8 and full.notes == ["the City box wants a city, not a state"]


def test_a_browser_search_can_be_an_address_with_the_words_in_it_or_none_at_all():
    by_address = parse_recipe(_browser(search={"how": "url", "url_template": f"{HOST}/s?q={{phrase}}&l={{place}}"}))
    assert by_address.search.url_template == f"{HOST}/s?q={{phrase}}&l={{place}}"
    assert parse_recipe(_browser(search={"how": "url", "url_template": f"{HOST}/s/{{phrase}}"})).search.how == "url"
    assert parse_recipe(_browser(search={"how": "none"})).search.phrase_box is None


def test_a_browser_address_with_a_public_id_in_its_path_is_no_token_but_one_in_its_query_is():
    key, portal = "0123456789abcdef" * 2, f"{HOST}/portal/" + "0123456789abcdef" * 2
    got = parse_recipe(_browser(start_url=f"{portal}/career-page", recommended_url=f"{portal}/for-you",
                                search={"how": "url", "url_template": f"{portal}/s?q={{phrase}}"}))
    assert key in got.start_url and key in got.search.url_template
    with pytest.raises(ValueError, match="start_url carries a token or key"):
        parse_recipe(_browser(start_url=f"{portal}/career-page?key={A_KEY}"))


@pytest.mark.parametrize(("raw", "reason"), [
    (_browser(start_url="http://localhost:8080/jobs"), "start_url must be a public http(s) address"),
    (_browser(start_url="ftp://jobs.example.test/x"), "start_url must be a public http(s) address"),
    (_browser(start_url="https://user:secret@jobs.example.test/x"), "start_url must be a public http(s) address"),
    (_browser(start_url=f"{HOST}/jobs?key={A_KEY}"), "start_url carries a token or key"),
    (_browser(recommended_url="http://10.0.0.1/for-you"), "recommended_url must be a public http(s) address"),
    (_browser(recommended_url=f"{HOST}/for-you?t={G_KEY}"),
     "recommended_url carries a token or key"),
    (_browser(search={"how": "url", "url_template": "http://192.168.1.5/s?q={phrase}"}),
     "url_template must be a public http(s) address"),
    (_browser(search={"how": "url", "url_template": f"{HOST}/s?q={{phrase}}&k={A_KEY}"}),
     "url_template carries a token or key"),
    (_browser(search={"how": "url", "url_template": "https://{phrase}.jobs.example.test/s"}),
     "url_template: a placeholder may not be in the host"),
    (_browser(search={"how": "url", "url_template": "https://jobs.example.test{place}/s?q={phrase}"}),
     "url_template: a placeholder may not be in the host"),
    (_browser(search={"how": "url", "url_template": f"{HOST}/s?l={{place}}"}), "url_template must hold {phrase}"),
    (_browser(search={"how": "url", "url_template": f"{HOST}/search"}), "url_template must hold {phrase}"),
    (_browser(search={"how": "url"}), "url_template is needed"),
    (_browser(search={"how": "boxes"}), "phrase_box is needed"),
    (_browser(search={"how": "scroll"}), "search.how"),
    (_browser(search={"how": "none", "script": "x"}), "search.script: is not part of the browser recipe"),
    (_browser(results=_results(fields={"title": "t", "salary": "s"})), "fields.salary is not a result field"),
    (_browser(results=_results(fields={"company": "c"})), "fields needs title"),
    (_browser(results=_results(next="")), "results.next: is empty"),
    (_browser(results=_results(row="x" * 301)), "results.row: is longer than 300 characters"),
    (_browser(results=_results(fields={"title": "x" * 301})), "results.fields.title: is longer than 300 characters"),
    (_browser(search=_search(phrase_box="x" * 301)), "search.phrase_box: is longer than 300 characters"),
    (_browser(search=_search(recency={"control": "menu", "choices": ["x" * 301]})),
     "search.recency.choices.0: is longer than 300 characters"),
    (_browser(posting={"description": "x" * 301}), "posting.description: is longer than 300 characters"),
    (_browser(notes=["x" * 301]), "notes.0: is longer than 300 characters"),
    (_browser(notes=["a note"] * 11), "notes: has more than 10 notes"),
    ({k: v for k, v in _browser().items() if k != "posting"}, "posting: is missing"),
    ({k: v for k, v in _browser().items() if k != "start_url"}, "start_url: is missing"),
    (_browser(code="x"), "code: is not part of the browser recipe"),
])
def test_a_bad_browser_recipe_is_refused_with_its_reason(raw, reason):
    with pytest.raises(ValueError) as caught:
        parse_recipe(raw)
    assert reason in str(caught.value)


def test_a_browser_recipe_over_six_thousand_characters_is_refused_though_each_text_is_short():
    wordy = _browser(
        search=_search(recency={"control": "the Date posted menu", "choices": ["c" * 300] * 10}),
        notes=["n" * 300] * 10)
    assert 6_000 < len(json.dumps(wordy)) < skills.RECIPE_MAX_BYTES
    with pytest.raises(ValueError, match="the recipe is longer than 6000 characters"):
        parse_recipe(wordy)
    wordy["notes"] = ["n" * 300] * 5
    assert parse_recipe(wordy).kind == "browser"


@pytest.mark.parametrize("pattern", [
    r"(a+)+$", r"(\w+\s?)*end", r"(?:jobs|careers)+/", r"(?=x)jobs", r"(?<!x)jobs", r"(a)\1", r"(a)?(?(1)b|c)",
    r".*a.*b.*c.*d",
])
def test_a_pattern_that_could_backtrack_without_end_is_refused(pattern):
    with pytest.raises(ValueError, match="not allowed|not a valid"):
        parse_recipe(_list_page(link_pattern=pattern))
    with pytest.raises(ValueError, match="next_pattern"):
        parse_recipe(_list_page(next_pattern=pattern))


@pytest.mark.parametrize("pattern", [
    r"/careers/jobs/\d+", r"^next$|^more", r"/(?:job|position)s?/[a-z0-9-]+", r".*/jobs/.*"])
def test_ordinary_patterns_are_accepted(pattern):
    assert parse_recipe(_list_page(link_pattern=pattern)).link_pattern == pattern


def test_public_url():
    assert skills.public_url("https://jobs.example.test/x?y=1")
    assert skills.public_url("http://93.184.216.34/x")
    assert not any(skills.public_url(u) for u in (None, "", 5, "jobs.example.test", "http://[::1]/x", "http:///x"))


# --- running a recipe -------------------------------------------------------------------------------------------------

KEYS = {"ext_id", "title", "location", "work_mode", "comp", "comp_min", "comp_max", "posted", "url", "apply_url",
        "text", "department"}


async def test_an_adapter_recipe_runs_the_job_systems_reader():
    fetch, seen = _fetcher(lambda request: httpx.Response(200, json={"jobs": [
        {"id": 7, "title": "Analyst", "location": {"name": "Remote"},
         "absolute_url": "https://job-boards.example.test/7"}]}))
    listing = await run_recipe(fetch, {"kind": "adapter", "ats": SYSTEM_A, "board": board_of("acme")})
    host = seen[0].url.host
    assert any(host == domain or host.endswith("." + domain) for domain in ats.SYSTEMS[SYSTEM_A].domains), host
    assert "acme" in str(seen[0].url)
    assert [p["title"] for p in listing.postings] == ["Analyst"]


async def test_an_adapter_that_fails_is_a_skill_error_and_a_refusal_passes_through():
    recipe = {"kind": "adapter", "ats": SYSTEM_A, "board": board_of("acme")}
    fetch, _ = _fetcher(lambda request: httpx.Response(500))
    with pytest.raises(SkillError, match=f"{LABEL_A} answered 500"):
        await run_recipe(fetch, recipe)
    fetch, _ = _fetcher(lambda request: httpx.Response(403))
    with pytest.raises(Refused):
        await run_recipe(fetch, recipe)


async def test_a_request_recipe_replays_the_data_request_and_reads_each_field():
    elsewhere = _job(2, loc={"name": ["Oakfield", "Remote"]}, path="https://apply.example.test/x/2")
    fetch, seen = _fetcher(_world([_job(1), elsewhere]))
    listing = await run_recipe(fetch, _request())
    assert [str(r.url) for r in seen] == [f"{HOST}/api/jobs"] and seen[0].method == "GET"
    one, two = listing.postings
    assert set(one) == KEYS
    assert (one["ext_id"], one["title"], one["location"], one["work_mode"]) == (
        f"{HOST}/jobs/1", "Role 1", "Remote - US", "remote")
    assert one["url"] == one["apply_url"] == f"{HOST}/jobs/1" and one["posted"] == "2026-10-01"
    assert one["text"] == "Do thing 1."
    assert two["location"] == "Oakfield, Remote" and two["url"] == "https://apply.example.test/x/2"
    assert not listing.truncated and listing.note is None


async def test_a_request_recipe_joins_a_bare_id_to_its_prefix_and_reads_pay_and_ids():
    rows = [{"ref": "A-9", "t": "Buyer", "slug": "buyer-9", "pay": "$80,000 - $100,000 per year", "dept": "Supply"}]
    fetch, _ = _fetcher(_world(rows))
    listing = await run_recipe(fetch, _request(
        fields={"title": "t", "url": "slug", "ext_id": "ref", "comp": "pay", "department": "dept"},
        url_prefix=f"{HOST}/jobs/"))
    [p] = listing.postings
    assert (p["ext_id"], p["url"], p["department"]) == ("A-9", f"{HOST}/jobs/buyer-9", "Supply")
    assert (p["comp_min"], p["comp_max"]) == (80000, 100000)


async def test_a_request_recipe_for_a_job_board_can_read_each_postings_company_and_only_then_has_one():
    rows = [_job(1, org={"name": "Umbrella Co"}), _job(2, org=None)]
    fetch, _ = _fetcher(_world(rows))
    mapped = await run_recipe(fetch, _request(fields={"title": "name", "url": "path", "company": "org"}))
    assert [p.get("company") for p in mapped.postings] == ["Umbrella Co", None]
    fetch, _ = _fetcher(_world(rows))
    plain = await run_recipe(fetch, _request())
    assert all("company" not in p for p in plain.postings), "a recipe that does not map it adds no key"


async def test_a_request_recipe_reads_a_list_at_the_top_of_the_answer():
    fetch, _ = _fetcher(lambda request: httpx.Response(200, json=[_job(1), "junk", _job(2)]))
    listing = await run_recipe(fetch, _request(items=""))
    assert [p["title"] for p in listing.postings] == ["Role 1", "Role 2"]


async def test_a_request_recipe_drops_rows_without_a_title_and_says_so_unless_asked_to_keep_them():
    rows = [_job(1), _job(2, name=None), _job(3, path=None)]
    fetch, _ = _fetcher(_world(rows))
    listing = await run_recipe(fetch, _request())
    assert [p["title"] for p in listing.postings] == ["Role 1", "Role 3"]
    assert listing.note == "1 rows had no title and were left out"
    fetch, _ = _fetcher(_world(rows))
    kept = await run_recipe(fetch, _request(), keep_bad=True)
    assert [p["title"] for p in kept.postings] == ["Role 1", None, "Role 3"] and kept.postings[2]["url"] is None


async def test_a_request_recipe_that_pages_by_offset_in_the_body_stops_on_a_short_page():
    everything = [_job(n) for n in range(1, 6)]

    def handle(request: httpx.Request) -> httpx.Response:
        asked = json.loads(request.content)
        start = asked["offset"]
        return httpx.Response(200, json={"data": {"results": everything[start:start + asked["limit"]]}})

    fetch, seen = _fetcher(handle)
    listing = await run_recipe(fetch, _request(
        method="POST", body={"q": "x"},
        page={"param": "offset", "size": 2, "in": "body", "size_name": "limit"}))
    assert [json.loads(r.content) for r in seen] == [
        {"q": "x", "offset": 0, "limit": 2}, {"q": "x", "offset": 2, "limit": 2}, {"q": "x", "offset": 4, "limit": 2}]
    assert [p["title"] for p in listing.postings] == [f"Role {n}" for n in range(1, 6)]
    assert not listing.truncated


async def test_a_request_recipe_that_pages_by_page_number_in_the_query_reads_to_the_last_page():
    everything = [_job(n) for n in range(1, 11)]

    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"data": {"results": everything[(page - 1) * 2:page * 2]}})

    fetch, seen = _fetcher(handle)
    listing = await run_recipe(fetch, _request(url=f"{HOST}/api/jobs?board=1", page={"param": "page", "size": 2}))
    assert [r.url.params["page"] for r in seen] == ["1", "2", "3", "4", "5", "6"], "to the empty page after the 5th"
    assert len(listing.postings) == 10 and not listing.truncated and listing.note is None


async def test_paging_stops_and_says_so_when_the_site_ignores_the_page_number():
    fetch, seen = _fetcher(_world([_job(1), _job(2)]))
    listing = await run_recipe(fetch, _request(page={"param": "page", "size": 2}))
    assert len(seen) == 2 and len(listing.postings) == 2, "the repeated page brings nothing and ends the read"
    assert listing.truncated and listing.note == "stopped at 2 postings (a page repeated one already read)"


async def test_a_run_has_no_cap_on_postings_or_pages():
    """It was 2000 postings and 50 pages: a site of 2,100 is 21 full pages and an empty one, all read."""
    everything = [_job(n) for n in range(1, 2101)]

    def handle(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"data": {"results": everything[offset:offset + 100]}})

    fetch, seen = _fetcher(handle)
    listing = await run_recipe(fetch, _request(page={"param": "offset", "size": 100}))
    assert len(seen) == 22 and len(listing.postings) == 2100 and not listing.truncated and listing.note is None


async def test_a_request_recipe_keeps_what_it_read_and_says_so_when_its_time_runs_out():
    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == 3:  # the time runs out as the third page is answered
            fetch.deadline = time.monotonic() - 1
        return httpx.Response(200, json={"data": {"results": [_job(2 * page - 1), _job(2 * page)]}})

    fetch, seen = _fetcher(handle)
    fetch.deadline = time.monotonic() + 3600
    listing = await run_recipe(fetch, _request(page={"param": "page", "size": 2}))
    assert len(seen) == 3 and len(listing.postings) == 6, "no fourth page is asked for"
    assert listing.truncated and listing.note == "stopped at 6 postings (the time allowed for the read ran out)"


async def test_a_request_recipe_asked_for_enough_stops_after_the_page_that_has_them():
    """A recipe's test needs `VERIFY_ROWS` postings, not the site's thousands (a try read 1,662)."""
    everything = [_job(n) for n in range(1, 101)]

    def handle(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"data": {"results": everything[offset:offset + 2]}})

    fetch, seen = _fetcher(handle)
    listing = await run_recipe(fetch, _request(page={"param": "offset", "size": 2}), enough=skills.VERIFY_ROWS)
    assert len(seen) == 2 and len(listing.postings) == 4, "the second page is the first to bring three"
    assert listing.truncated and listing.note == "stopped at 4 postings (enough for a test of the recipe)"


async def test_a_list_page_recipe_asked_for_enough_stops_after_the_page_that_has_them():
    def handle(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("p", "1"))
        links = "".join(f'<a href="/jobs/{2 * n - 1}">Role</a><a href="/jobs/{2 * n}">Role</a>')
        return httpx.Response(200, text=f'{links}<a href="/list?p={n + 1}">Next</a>')

    fetch, seen = _fetcher(handle)
    recipe = {"kind": "list_page", "url": f"{HOST}/list", "link_pattern": r"/jobs/\d+", "next_pattern": r"/list\?p=\d+",
              "title_from": "link_text"}
    listing = await run_recipe(fetch, recipe, enough=skills.VERIFY_ROWS)
    assert len(seen) == 2 and len(listing.postings) == 4 and listing.truncated


async def test_a_request_recipe_that_stops_still_says_how_many_rows_it_left_out_for_want_of_a_title():
    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        rows = [_job(2 * page - 1, name=None), _job(2 * page)]
        if page == 2:
            fetch.deadline = time.monotonic() - 1
        return httpx.Response(200, json={"data": {"results": rows}})

    fetch, seen = _fetcher(handle)
    listing = await run_recipe(fetch, _request(page={"param": "page", "size": 2}))
    assert [p["title"] for p in listing.postings] == ["Role 2", "Role 4"]
    assert listing.note == ("stopped at 2 postings (the time allowed for the read ran out); "
                            "2 rows had no title and were left out")


async def test_a_request_recipe_says_what_changed_when_the_site_does_not_answer_as_before():
    fetch, _ = _fetcher(lambda request: httpx.Response(200, json={"data": {"jobs": []}}))
    with pytest.raises(SkillError, match="no list found at 'data.results'"):
        await run_recipe(fetch, _request())
    fetch, _ = _fetcher(lambda request: httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(SkillError, match="did not answer with JSON"):
        await run_recipe(fetch, _request())
    fetch, _ = _fetcher(lambda request: httpx.Response(503))
    with pytest.raises(SkillError, match="jobs.example.test answered 503"):
        await run_recipe(fetch, _request())


async def test_robots_txt_and_a_refusal_stop_a_request_recipe():
    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\nDisallow: /api/") if request.url.path == "/robots.txt" \
            else httpx.Response(200, json={})

    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0)
    with pytest.raises(Refused, match="robots.txt"):
        await run_recipe(fetch, _request())


async def test_a_list_page_recipe_reads_the_links_and_follows_next_to_the_last_page():
    pages = {"/careers": LIST_ONE, "/careers?p=2": LIST_TWO, "/careers?p=3": LIST_THREE}

    def handle(request: httpx.Request) -> httpx.Response:
        key = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        return httpx.Response(200, text=pages[key]) if key in pages else httpx.Response(404)

    fetch, seen = _fetcher(handle)
    listing = await run_recipe(fetch, _list_page())
    assert len(seen) == 3
    assert [(p["title"], p["url"]) for p in listing.postings] == [
        ("Data Analyst", f"{HOST}/careers/jobs/101-analyst"), ("Team Lead", f"{HOST}/careers/jobs/102-lead"),
        ("Program Manager", f"{HOST}/careers/jobs/104-pm")]
    assert listing.note == "1 links had no title" and not listing.truncated and set(listing.postings[0]) == KEYS

    fetch, _ = _fetcher(handle)
    kept = await run_recipe(fetch, _list_page(), keep_bad=True)
    assert [p["title"] for p in kept.postings] == ["Data Analyst", "Team Lead", None, "Program Manager"]

    fetch, _ = _fetcher(handle)
    by_attr = await run_recipe(fetch, _list_page(title_from="title_attr"))
    assert [p["title"] for p in by_attr.postings] == ["Ops Manager"]


async def test_a_list_page_recipe_does_not_follow_a_next_link_off_the_site_or_back_to_a_page_it_read():
    page = '<a href="/careers/jobs/1">One</a><a href="https://elsewhere.example.test/p2">Next</a>'
    fetch, seen = _fetcher(lambda request: httpx.Response(200, text=page))
    listing = await run_recipe(fetch, _list_page())
    assert len(seen) == 1 and [p["title"] for p in listing.postings] == ["One"]
    loop = '<a href="/careers/jobs/1">One</a><a href="/careers">Next</a>'
    fetch, seen = _fetcher(lambda request: httpx.Response(200, text=loop))
    await run_recipe(fetch, _list_page())
    assert len(seen) == 1


async def test_a_list_page_recipe_follows_a_long_chain_of_pages_to_the_end():
    """It was fifty pages: a thousand-posting list of twenty-five pages is no problem, and neither is a longer one."""
    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("p", "1"))
        nxt = f'<a href="/careers?p={page + 1}">Next</a>' if page < 120 else ""
        return httpx.Response(200, text=f'<a href="/careers/jobs/{page}">Role {page}</a>{nxt}')

    fetch, seen = _fetcher(handle)
    listing = await run_recipe(fetch, _list_page())
    assert len(seen) == 120 and len(listing.postings) == 120 and not listing.truncated and listing.note is None


async def test_a_list_page_recipe_stops_and_says_so_when_the_pages_go_round_in_circles():
    """A site that ignores the page number answers the same links under every next address."""
    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("p", "1"))
        return httpx.Response(200, text=f'<a href="/careers/jobs/1">One</a><a href="/careers?p={page + 1}">Next</a>')

    fetch, seen = _fetcher(handle)
    listing = await run_recipe(fetch, _list_page())
    assert len(seen) == 2 and [p["title"] for p in listing.postings] == ["One"]
    assert listing.truncated and listing.note == "stopped at 1 posting (a page repeated one already read)"


async def test_a_list_page_recipe_ends_at_a_page_with_no_links_and_that_is_not_a_cut_read():
    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("p", "1"))
        links = '<a href="/careers/jobs/7">Seven</a>' if page == 1 else ""
        return httpx.Response(200, text=f'{links}<a href="/careers?p={page + 1}">Next</a>')

    fetch, seen = _fetcher(handle)
    listing = await run_recipe(fetch, _list_page())
    assert len(seen) == 2 and [p["title"] for p in listing.postings] == ["Seven"] and not listing.truncated


async def test_a_list_page_recipe_keeps_what_it_read_and_says_so_when_its_time_runs_out():
    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("p", "1"))
        if page == 2:
            fetch.deadline = time.monotonic() - 1
        return httpx.Response(200, text=f'<a href="/careers/jobs/{page}">Role {page}</a>'
                                        f'<a href="/careers?p={page + 1}">Next</a>')

    fetch, seen = _fetcher(handle)
    fetch.deadline = time.monotonic() + 3600
    listing = await run_recipe(fetch, _list_page())
    assert len(seen) == 2 and [p["title"] for p in listing.postings] == ["Role 1", "Role 2"]
    assert listing.truncated and listing.note == "stopped at 2 postings (the time allowed for the read ran out)"


async def test_a_list_page_that_answers_with_an_error_is_a_skill_error():
    fetch, _ = _fetcher(lambda request: httpx.Response(404))
    with pytest.raises(SkillError, match="answered 404"):
        await run_recipe(fetch, _list_page())


async def test_the_matching_budget_stops_a_read_that_spends_too_long(monkeypatch):
    monkeypatch.setattr(skills, "REGEX_BUDGET_SECONDS", -1.0)
    fetch, _ = _fetcher(lambda request: httpx.Response(200, text=LIST_ONE))
    with pytest.raises(SkillError, match="took too long"):
        await run_recipe(fetch, _list_page())


def test_a_pattern_the_shape_check_lets_through_is_stopped_by_the_timeout_not_waited_for(monkeypatch):
    """`(?:a*){0,10}[b-c]` is no repeat inside a long repeat, so the shape check accepts it, and `re` takes
    longer than the age of the page on 200 letters. Matching it runs against the budget and is stopped."""
    pattern = r"(?:a*){0,10}[b-c]"
    assert parse_recipe(_list_page(link_pattern=pattern)).link_pattern == pattern
    monkeypatch.setattr(skills, "REGEX_BUDGET_SECONDS", 0.3)
    budget = skills._Budget()
    started = time.perf_counter()
    with pytest.raises(SkillError, match="took too long"):
        budget.search(skills.compile_pattern(pattern), "a" * 200)
    assert time.perf_counter() - started < 3
    assert skills._Budget().search(skills.compile_pattern(r"/careers/jobs/\d+"), "/careers/jobs/101") is True


async def test_a_browser_recipe_is_saved_but_is_the_browsers_to_run_so_the_app_never_runs_it():
    fetch, seen = _fetcher(lambda request: httpx.Response(200, text="hello"))
    with pytest.raises(SkillError, match="a browser skill is the browser's to run: the night's read leaves this site"):
        await run_recipe(fetch, _browser())
    with pytest.raises(SkillError, match="the browser's to run"):
        await run_recipe(fetch, parse_recipe(_browser()))
    assert seen == []


async def test_run_recipe_refuses_a_recipe_that_is_not_valid():
    fetch, seen = _fetcher(lambda request: httpx.Response(200))
    with pytest.raises(ValueError, match="unknown recipe kind"):
        await run_recipe(fetch, {"kind": "shell"})
    assert seen == []


# --- versions ---------------------------------------------------------------------------------------------------------


async def _make_site(session, owner: str = OWNER, url: str = f"{HOST}/") -> Site:
    site = Site(owner=owner, url=url, kind="employer", created_at=datetime.now(UTC))
    session.add(site)
    await session.flush()
    return site


async def test_save_skill_numbers_the_versions_keeps_the_history_and_current_is_the_newest_live_one(js):
    async with db.session_factory()() as session:
        site = await _make_site(session)
        assert await current_skill(session, site.id) is None
        one = await save_skill(session, OWNER, site.id, _list_page(), None)
        two = await save_skill(session, OWNER, site.id, _request(), None)
        assert (one.version, two.version) == (1, 2)
        assert two.recipe["kind"] == "request" and two.recipe["fields"]["title"] == "name"
        assert (await current_skill(session, site.id)).version == 2
        two.retired_at = datetime.now(UTC)
        await session.flush()
        assert (await current_skill(session, site.id)).version == 1
        three = await save_skill(session, OWNER, site.id, _request(), None)
        assert three.version == 3 and (await current_skill(session, site.id)).version == 3
        await session.commit()


async def test_a_browser_recipe_is_saved_as_the_next_version_and_a_broken_one_is_not(js):
    async with db.session_factory()() as session:
        site = await _make_site(session)
        one = await save_skill(session, OWNER, site.id, _list_page(), None)
        two = await save_skill(session, OWNER, site.id, _browser(notes=["the City box wants a city"]), None)
        assert (one.version, two.version) == (1, 2)
        assert two.recipe["kind"] == "browser" and two.recipe["search"]["how"] == "boxes"
        assert two.recipe["notes"] == ["the City box wants a city"]
        assert (await current_skill(session, site.id)).version == 2
        with pytest.raises(ValueError, match="fields needs title"):
            await save_skill(session, OWNER, site.id, _browser(results=_results(fields={"company": "c"})), None)
        assert (await current_skill(session, site.id)).version == 2
        assert parse_recipe((await current_skill(session, site.id)).recipe).kind == "browser", "it reads back whole"
        await session.rollback()


async def test_a_saved_recipe_can_be_read_back_and_run(js):
    async with db.session_factory()() as session:
        site = await _make_site(session)
        skill = await save_skill(
            session, OWNER, site.id, _request(page={"param": "offset", "size": 10, "in": "query"}), None)
        await session.commit()
        recipe = skill.recipe
    assert recipe["page"]["in"] == "query"
    fetch, _ = _fetcher(_world([_job(1)]))
    assert [p["title"] for p in (await run_recipe(fetch, recipe)).postings] == ["Role 1"]


async def test_save_skill_refuses_a_bad_recipe_and_a_site_that_is_not_the_owners(js):
    async with db.session_factory()() as session:
        site = await _make_site(session)
        with pytest.raises(ValueError, match="fields needs title"):
            await save_skill(session, OWNER, site.id, _request(fields={"url": "path"}), None)
        with pytest.raises(ValueError, match="no such site"):
            await save_skill(session, "someone-else", site.id, _request(), None)
        with pytest.raises(ValueError, match="no such site"):
            await save_skill(session, OWNER, uuid.uuid4(), _request(), None)
        assert await current_skill(session, site.id) is None
        await session.rollback()


# --- the Check --------------------------------------------------------------------------------------------------------


async def _site_with(recipe: dict | None, *, project: bool = False) -> dict:
    """A site that has `recipe` saved as its skill; returns the check's data pointing at it."""
    async with db.session_factory()() as session:
        site = await _make_site(session)
        if recipe is not None:
            await save_skill(session, OWNER, site.id, recipe, None)
        data = {"owner": OWNER, "site": str(site.id), "item": {"key": "read-listings", "title": "Read the listings"}}
        if project:
            row = Project(owner=OWNER, site_id=site.id, title="Learn the site", outcome="Its listings can be read")
            session.add(row)
            await session.flush()
            data = {"owner": OWNER, "project": str(row.id), "item": data["item"]}
        await session.commit()
    return data


def _use(monkeypatch, handle):
    """Each check makes its own fetcher (as `polite.make` does); returns them as they are made, and every request."""
    made: list[Fetcher] = []
    seen: list[httpx.Request] = []

    def make() -> Fetcher:
        made.append(_fetcher(handle, seen)[0])
        return made[-1]

    monkeypatch.setattr(polite, "make", make)
    return made, seen


async def _check(data: dict) -> dict:
    async with db.session_factory()() as session:
        return await site_check(session, None, data)


def _with_item(check: dict, **more) -> dict:
    return {**check, "item": {**check["item"], **more}}


async def test_a_good_skill_is_on_plan_and_the_gateway_reads_it_and_the_fetcher_is_closed(js, monkeypatch):
    data = await _site_with(_request())
    made, seen = _use(monkeypatch, _world())
    found = await _check(data)
    assert found["on_plan"] is True and found["difference"] == "" and found["verdict"] == "on_plan"
    assert found["found"] == [] and found["count"] == 4 and found["skill_version"] == 1
    assert len(found["sampled"]) == 2 and made[0].client.is_closed
    assert [r.url.path for r in seen if r.url.path.startswith("/jobs/")] == ["/jobs/1", "/jobs/3"]


async def test_the_check_reads_with_a_time_limit(js, monkeypatch):
    data = await _site_with(_request())
    made, _ = _use(monkeypatch, _world())
    await _check(data)
    assert 0 < made[0].deadline - time.monotonic() <= skills.TRY_SECONDS, "a long site is cut short, not waited for"


async def test_a_check_read_cut_short_by_its_time_says_so_beside_the_count(js, monkeypatch):
    data = await _site_with(_request(page={"param": "page", "size": 2}))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/jobs":
            page = int(request.url.params["page"])
            if page == 2:
                made[0].deadline = time.monotonic() - 1  # the time runs out as the second page is answered
            return httpx.Response(200, json={"data": {"results": [_job(2 * page - 1), _job(2 * page)]}})
        return httpx.Response(200, text="a posting")

    made, _ = _use(monkeypatch, handle)
    found = await _check(_with_item(data, result={"expected_count": 40}))
    assert found["on_plan"] is False and found["count"] == 4
    assert found["found"][0]["detail"] == "4 came back, 40 were expected (the read was cut short)"


async def test_the_site_can_come_from_the_project_given_as_its_id(js, monkeypatch):
    data = await _site_with(_request(), project=True)
    _use(monkeypatch, _world())
    assert (await _check(data))["on_plan"] is True
    assert (await _check({**data, "project": {"id": data["project"]}}))["on_plan"] is True
    async with db.session_factory()() as session:
        row = await session.get(Project, uuid.UUID(data["project"]))
    assert (await _check({**data, "project": {"site_id": str(row.site_id)}}))["on_plan"] is True


async def test_nothing_coming_back_is_off_plan(js, monkeypatch):
    data = await _site_with(_request())
    _use(monkeypatch, _world(rows=[]))
    found = await _check(data)
    assert found["on_plan"] is False and found["verdict"] == "off_plan" and found["count"] == 0
    assert found["found"][0]["what"] == "Nothing came back"
    assert found["difference"].startswith("Nothing came back: ")


async def test_nothing_coming_back_is_on_plan_when_the_item_says_the_site_has_none(js, monkeypatch):
    data = await _site_with(_request())
    _use(monkeypatch, _world(rows=[]))
    assert (await _check(_with_item(data, result={"expected_count": 0})))["on_plan"] is True
    _use(monkeypatch, _world())
    found = await _check(_with_item(data, result={"expected_count": 0}))
    assert found["on_plan"] is False and "says has none" in found["difference"]


async def test_postings_with_no_title_or_no_link_are_off_plan(js, monkeypatch):
    data = await _site_with(_request())
    _use(monkeypatch, _world([_job(1), _job(2, path=None), _job(3, name=None, path="/jobs/3"), _job(4, path=None)]))
    found = await _check(data)
    assert found["on_plan"] is False
    whats = {f["what"]: f["detail"] for f in found["found"]}
    assert whats["Postings with no title"] == "1 of 4"
    assert whats["Postings with no link"].startswith("2 of 4, such as ") and "Role 2" in whats["Postings with no link"]
    assert found["difference"].startswith("Postings with no title: 1 of 4; Postings with no link")


@pytest.mark.parametrize(("expected", "ok"), [(4, True), (5, True), (6, True), (7, False), (2, True), (1, False),
                                              ("4", True), ("40", False)])
async def test_the_count_must_be_near_the_one_the_item_expected(js, monkeypatch, expected, ok):
    data = await _site_with(_request())
    _use(monkeypatch, _world())
    found = await _check(_with_item(data, result={"expected_count": expected}))
    assert found["on_plan"] is ok
    if not ok:
        assert found["found"][0]["what"] == "The number of postings is not what the item expected"
        assert "4 came back" in found["found"][0]["detail"]


async def test_ten_percent_is_allowed_on_a_big_count(js, monkeypatch):
    data = await _site_with(_request())
    _use(monkeypatch, _world([_job(n) for n in range(1, 101)]))
    for expected, ok in ((100, True), (108, True), (111, True), (113, False), (92, True), (88, False)):
        found = await _check(_with_item(data, result={"expected_count": expected}))
        assert found["on_plan"] is ok, expected


def test_the_expected_count_is_the_one_the_item_reports_in_its_result():
    cases = [{"item": {"result": {"expected_count": 7}}}, {"item": {"result": {"expected_count": "7"}}},
             {"item": {"result": {"expected_count": 7.0}}}]
    assert [expected_count(c) for c in cases] == [7] * len(cases)
    assert expected_count({"item": {"evidence": "The page says 7 roles (expected_count: 7)."}}) is None
    assert expected_count({"item": {"result": {"count": 7}}}) is None and expected_count({}) is None
    assert expected_count({"item": {"result": {"expected_count": -1}}}) is None
    assert expected_count({"item": {"result": {"expected_count": True}}}) is None


async def test_an_item_that_went_off_plan_is_off_plan_with_its_own_reason_and_nothing_is_fetched(js, monkeypatch):
    data = await _site_with(_request())
    made, seen = _use(monkeypatch, _world())
    found = await _check(_with_item(data, status="off_plan", outcome_reason="The page needs a sign-in"))
    assert found["on_plan"] is False and found["difference"] == "The page needs a sign-in" and seen == []
    assert (await _check(_with_item(data, status="done")))["on_plan"] is True


async def test_a_sample_link_that_does_not_open_is_off_plan(js, monkeypatch):
    data = await _site_with(_request())
    _use(monkeypatch, _world(job_status=404))
    found = await _check(data)
    assert found["on_plan"] is False and found["count"] == 4
    assert [f["what"] for f in found["found"]] == ["A sample link did not open"] * 2
    assert f"{HOST}/jobs/1 answered 404" in found["found"][0]["detail"]


async def test_a_sample_link_the_site_refuses_is_off_plan(js, monkeypatch):
    data = await _site_with(_request())
    world = _world()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403) if request.url.path.startswith("/jobs/") else world(request)

    _use(monkeypatch, handle)
    found = await _check(data)
    assert found["on_plan"] is False and found["found"][0]["what"] == "A sample link was refused"


async def test_only_two_links_are_opened_and_never_a_private_one(js, monkeypatch):
    data = await _site_with(_request())
    rows = [_job(n) for n in range(1, 31)] + [_job(31, path="http://10.0.0.5/jobs/31")]
    _, seen = _use(monkeypatch, _world(rows))
    await _check(data)
    assert len([r for r in seen if r.url.path.startswith("/jobs/")]) == 2
    _, seen = _use(monkeypatch, _world([_job(1, path="http://127.0.0.1/jobs/1")]))
    assert (await _check(data))["on_plan"] is True
    assert [r.url.path for r in seen if r.url.path.startswith("/jobs/")] == []


async def test_a_site_that_changed_shape_or_asks_not_to_be_read_is_off_plan(js, monkeypatch):
    data = await _site_with(_request())
    _use(monkeypatch, lambda request: httpx.Response(200, json={"oops": 1}))
    found = await _check(data)
    assert found["on_plan"] is False and "no list found" in found["difference"]
    made, _ = _use(monkeypatch, lambda request: httpx.Response(403))
    found = await _check(data)
    assert found["on_plan"] is False and "asks not to be read" in found["difference"] and made[0].client.is_closed

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    _use(monkeypatch, down)
    found = await _check(data)
    assert found["on_plan"] is False and "did not answer" in found["difference"]


async def test_a_site_with_no_skill_a_broken_skill_or_no_site_is_off_plan_and_nothing_is_fetched(js, monkeypatch):
    data = await _site_with(None)
    _, seen = _use(monkeypatch, _world())
    found = await _check(data)
    assert found["on_plan"] is False and "no saved skill" in found["difference"]
    async with db.session_factory()() as session:
        site = await _make_site(session, url=f"{HOST}/other")
        session.add(SiteSkill(owner=OWNER, site_id=site.id, version=1, recipe={"kind": "shell"},
                              created_at=datetime.now(UTC)))
        await session.commit()
    found = await _check({**data, "site": str(site.id)})
    assert found["on_plan"] is False and "not a recipe the app can run" in found["difference"]
    found = await _check({"owner": OWNER, "item": {}})
    assert found["on_plan"] is False and "does not say which site" in found["difference"]
    assert seen == []


async def test_an_item_that_went_off_plan_is_off_plan_with_its_reason_and_nothing_is_fetched(js, monkeypatch):
    data = await _site_with(_request())
    _, seen = _use(monkeypatch, _world())
    reason = "The page loads more on scroll" + "!" * 3000
    found = await _check(_with_item(data, status="off_plan", outcome_reason=reason))
    assert found["on_plan"] is False and found["difference"].startswith("The page loads more on scroll")
    assert len(found["difference"]) == 2000 and seen == []


async def test_the_check_reads_the_newest_version_of_the_skill(js, monkeypatch):
    data = await _site_with(_request())
    async with db.session_factory()() as session:
        await save_skill(session, OWNER, uuid.UUID(data["site"]), _list_page(), None)
        await session.commit()
    page = '<a href="/careers/jobs/1">One</a><a href="/careers/jobs/2">Two</a>'
    _use(monkeypatch, lambda request: httpx.Response(200, text=page))
    found = await _check(data)
    assert found["skill_version"] == 2 and found["count"] == 2


async def test_act_does_not_keep_a_browser_recipe_for_a_project(js):
    """A browser recipe is the owner's browser session's to write (docs/PRD.md 37.1): the scout's Act refuses it with
    the words skill.try uses."""
    from app.sites.base import PlanEngineError

    async with db.session_factory()() as session:
        site = await _make_site(session)
        project = Project(owner=OWNER, site_id=site.id, title="Learn the site", outcome="Its listings can be read")
        session.add(project)
        await session.flush()
        with pytest.raises(PlanEngineError, match="not by the app"):
            await skills.keep_skill(session, project, _browser())
        assert await current_skill(session, site.id) is None
        kept = await skills.keep_skill(session, project, _request())
        assert kept.version == 1


def test_every_problem_with_a_recipe_is_named_not_only_the_first():
    words = skills.recipe_problems(_browser(
        start_url="ftp://jobs.example.test/x", results=_results(fields={"company": "c"}), code="x"))
    assert len(words) == 3 and any("start_url" in w for w in words) and any("fields needs title" in w for w in words)
    assert skills.recipe_problems(_browser()) == []
    assert skills.recipe_problems("text") == ["a recipe is a JSON object"]
    assert skills.recipe_problems({"kind": "script"})[0].startswith("unknown recipe kind")
