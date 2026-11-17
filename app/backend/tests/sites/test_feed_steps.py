"""A list that is asked for through a page first, with a JSON body, and made of columns (`feed_schema` `steps`, `post`,
`headers`, `unavailable`, `paging.counts`, `paging.total_required`, the values `cell`, `cells` and `date_cell`, and a
detail read from `page_encoded_list`): each item is there for a problem a portal has that the plain feed could not say,
and each test puts that problem to a made-up portal (`portal.example.test`) behind a mock transport. Nothing here names
a real site."""

import json
from urllib.parse import quote

import httpx
import pytest

from app.jobsearch.adapters import AdapterError
from app.jobsearch.adapters.feeds import adapter_for
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.postings.index import fault_kind
from app.jobsearch.states import FetchKind
from app.sites import feed_schema
from app.sites.schema import Runbook

HOSTS = ("example.test",)
BOARD = {"host": "acme.portal.example.test", "section": "ex"}
SEARCH = "<html><script>var o = { portalNo: '4455', lang: 'en' };</script></html>"

READ = {
    "kind": "request",
    "headers": {"Accept-Encoding": "identity"},
    "steps": [{"get": "https://{host}/sections/{section}/search?lang=en",
               "capture": {"portal": r"portalNo\s*:\s*'(\d+)'"}}],
    "url": "https://{host}/rest/search?portal={portal}",
    "post": {"body": {"sort": "date", "filters": {"open": True}}, "numbers": {"pageNo": "page"},
             "headers": {"tz": "UTC"}},
    "paging": {"param": "page", "size": 25, "total": "paging.count", "counts": "kept", "total_required": True},
    "items": "rows",
    "unavailable": {"path": "shut", "says": "this {who} portal is shut"},
    "board_parts": {"section": "[A-Za-z0-9_.-]+"},
    "fields": {
        "ext_id": ["code", "id"],
        "title": {"cell": {"of": "cols", "at": "titleAt"}},
        "location": {"cells": {"of": "cols", "at": "placeAts", "dashed_places": True}},
        "posted": {"date_cell": {"of": "cols", "not_at": ["titleAt"], "not_in": ["placeAts"]}},
        "url": {"template": "https://{host}/sections/{section}/view?id={ext_id}"}},
    "work_mode": {},
    "detail": {"url": "https://{host}/sections/{section}/view?id={ext_id}", "answer": "page_encoded_list",
               "list_pattern": r"fill\('desc', \[(.*?)\]\);", "marker": "@@", "any_id": True},
}


def runbook(**over) -> Runbook:
    return Runbook.model_validate({
        "schema": 1, "key": "example_portal", "name": "Example Portal", "kind": "job_system", "hosts": list(HOSTS),
        "order": 91, "flags": {"needs": ["host", "section"], "customer_host": True}, "read": {**READ, **over}})


def reader(**over):
    return adapter_for(runbook(**over))


def row(n: int, places='["US-OH-Oakfield"]', **more) -> dict:
    return {"code": f"R{n}", "titleAt": 0, "placeAts": [1], "cols": [f"Role {n}", places, "Oct 7, 2026"], **more}


def page(rows: list, total: int, **more) -> dict:
    return {"rows": rows, "paging": {"count": total}, "shut": False, **more}


class Portal:
    """The made-up portal: its search page, the list it answers by page number, and what it was asked."""

    def __init__(self, pages: dict[int, dict] | None = None, search: str = SEARCH, search_status: int = 200,
                 where: str | None = None) -> None:
        self.pages, self.search, self.search_status, self.where = pages or {}, search, search_status, where
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        self.requests.append(request)
        if request.method == "GET" and request.url.path == "/sections/ex/search":
            if self.where:
                return httpx.Response(302, headers={"location": self.where})
            return httpx.Response(self.search_status, text=self.search)
        if request.method == "GET":
            return httpx.Response(200, text="<html>elsewhere</html>")
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json=self.pages[self.bodies[-1]["pageNo"]])

    def fetcher(self) -> Fetcher:
        return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(self.handle), follow_redirects=True),
                       per_host_seconds=0)


# --- a value found on one page that the next needs ----------------------------------------------------------------


async def test_a_value_the_search_page_gives_is_in_the_address_of_the_list_and_nothing_else_is_asked_first():
    portal = Portal({1: page([row(1)], 1)})
    listing = await reader().read(portal.fetcher(), BOARD)
    first, second = portal.requests
    assert (first.method, first.url.host, first.url.path) == ("GET", "acme.portal.example.test", "/sections/ex/search")
    assert (second.method, second.url.path, second.url.params["portal"]) == ("POST", "/rest/search", "4455")
    assert [p["ext_id"] for p in listing.postings] == ["R1"]


@pytest.mark.parametrize("search", ["<html>no portal here</html>", "portalNo: 'a b'", "portalNo: '" + "9" * 101 + "'"])
async def test_a_page_that_does_not_give_the_value_or_gives_one_that_is_no_short_plain_word_is_a_changed_shape(search):
    with pytest.raises(AdapterError, match="Example Portal changed shape"):
        await reader().read(Portal(search=search).fetcher(), BOARD)


async def test_a_page_answered_from_a_host_that_is_none_of_the_runbooks_says_the_board_has_moved_away():
    for where in ("https://careers.elsewhere.test/jobs", "https://example.test/", "https://example.test.evil.test/x"):
        with pytest.raises(AdapterError, match="answered 404") as raised:
            await reader().read(Portal(where=where).fetcher(), BOARD)
        assert raised.value.status == 404 and fault_kind(raised.value) == FetchKind.GONE


async def test_a_page_that_answers_an_error_fails_with_its_status():
    with pytest.raises(AdapterError, match="answered 503") as raised:
        await reader().read(Portal(search_status=503).fetcher(), BOARD)
    assert raised.value.status == 503


async def test_the_headers_of_the_read_go_with_every_request_and_the_lists_own_with_the_list_alone():
    portal = Portal({1: page([row(1)], 1)})
    await reader().read(portal.fetcher(), BOARD)
    search, listed = portal.requests
    assert search.headers["accept-encoding"] == "identity" and "tz" not in search.headers
    assert listed.headers["accept-encoding"] == "identity" and listed.headers["tz"] == "UTC"


# --- the list asked with a body -----------------------------------------------------------------------------------


async def test_the_page_number_goes_in_the_body_and_a_board_is_read_to_the_total_it_says_of_the_postings_kept():
    portal = Portal({1: page([row(1), row(2)], 3), 2: page([row(3)], 3)})
    listing = await reader().read(portal.fetcher(), BOARD)
    assert portal.bodies == [{"sort": "date", "filters": {"open": True}, "pageNo": 1},
                             {"sort": "date", "filters": {"open": True}, "pageNo": 2}]
    assert [p["ext_id"] for p in listing.postings] == ["R1", "R2", "R3"] and not listing.truncated


async def test_a_page_that_repeats_one_read_stops_the_read_and_says_so():
    same = page([row(1), row(2)], 60)
    listing = await reader().read(Portal({1: same, 2: same, 3: same}).fetcher(), BOARD)
    assert listing.truncated and listing.note == "stopped at 2 of 60 postings (a page repeated one already read)"


async def test_a_first_answer_with_no_total_is_a_changed_shape_only_where_the_total_is_required():
    answer = {"rows": [row(1)]}
    with pytest.raises(AdapterError, match="changed shape"):
        await reader().read(Portal({1: answer}).fetcher(), BOARD)
    loose = {**READ["paging"], "total_required": False, "counts": "pages"}
    listing = await reader(paging=loose).read(Portal({1: answer}).fetcher(), BOARD)
    assert [p["ext_id"] for p in listing.postings] == ["R1"]


async def test_a_flag_that_says_the_board_is_shut_fails_the_read_in_the_runbooks_words():
    with pytest.raises(AdapterError, match="this Example Portal portal is shut"):
        await reader().read(Portal({1: page([], 0, shut=True)}).fetcher(), BOARD)


# --- a posting made of columns -----------------------------------------------------------------------------------


def posting(raw: dict) -> dict:
    return reader().parse_item(raw, BOARD)


def test_the_row_says_which_column_is_its_title_and_which_its_places_and_the_first_other_day_is_its_date():
    got = posting(row(1, '["US-OH-Oakfield", "India-Pune"]'))
    assert (got["title"], got["location"], got["posted"]) == ("Role 1", "Oakfield, OH, US; Pune, India", "2026-10-07")
    assert got["url"] == "https://acme.portal.example.test/sections/ex/view?id=R1"
    moved = {"code": "R2", "titleAt": 2, "placeAts": [0], "cols": ["US-OH-Oakfield", "Oct 1, 2026", "Role 2"]}
    got = posting(moved)
    assert (got["title"], got["location"], got["posted"]) == ("Role 2", "Oakfield, OH, US", "2026-10-01")


@pytest.mark.parametrize(("places", "expected"), [
    ('["Multiple Locations"]', "Multiple Locations"), ("Remote", "Remote"), ("[bad", "[bad"), ("[]", None),
    ('["", "  "]', None), ("[1, null]", None), ('["A-B-C-D"]', "C-D, B, A"), ('["US-NY"]', "NY, US"), (None, None)])
def test_a_place_written_with_dashes_alone_or_as_a_list_is_read_the_way_people_say_it(places, expected):
    assert posting(row(1, places))["location"] == expected


@pytest.mark.parametrize("raw", [
    {"code": "R1", "cols": ["T"], "titleAt": 5}, {"code": "R1", "cols": "x"}, {"code": "R1"},
    {"code": "R1", "cols": ["T", "p"], "titleAt": "x", "placeAts": "y"},
    {"code": "R1", "cols": ["T", "p"], "placeAts": 7},  # a field that is no list of positions is no places
])
def test_a_row_whose_columns_or_positions_are_not_what_it_says_is_a_posting_with_what_is_there(raw):
    got = posting(raw)
    assert got["ext_id"] == "R1" and got["location"] is None and got["posted"] is None


def test_the_position_a_row_leaves_out_is_the_first_column_and_a_posting_has_the_first_present_of_its_ids():
    got = posting({"id": "X9", "cols": ["First", "p", "2026-10-02"]})
    assert (got["ext_id"], got["title"], got["posted"]) == ("X9", "First", "2026-10-02")
    assert posting({"code": "A", "id": "B", "cols": ["T"]})["ext_id"] == "A"


# --- one posting's page -------------------------------------------------------------------------------------------


def desc_page(*items: str) -> str:
    return "<script>fill('desc', ['1','true'," + ",".join(f"'{i}'" for i in items) + "]);</script>"


async def read_detail(text: str, ext_id: object = "R1", status: int = 200) -> dict:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.url.path == "/sections/ex/view" and request.url.params["id"] == str(ext_id)
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(status, text=text)

    fetch = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handle)), per_host_seconds=0)
    return await reader().detail(fetch, BOARD, {"ext_id": ext_id})


async def test_the_text_a_page_fills_in_from_a_script_list_is_its_marked_items_decoded_and_made_plain():
    one = quote("<p>Lead the work &amp; the team.</p><ul><li>Plan</li></ul>", safe="")
    two = quote("<p>Pay: $120,000</p>", safe="")
    got = await read_detail(desc_page("@@" + one, "@@" + two, "Oakfield"))
    assert got == {"text": "Lead the work & the team.\n- Plan\n\nPay: $120,000"}


@pytest.mark.parametrize("text", ["<html>nothing</html>", desc_page("a", "b"), desc_page("@@"), ""])
async def test_a_page_with_no_list_or_no_marked_text_is_a_changed_shape(text):
    with pytest.raises(AdapterError, match="changed shape"):
        await read_detail(text)


async def test_an_id_that_is_any_text_is_asked_for_percent_encoded_and_one_that_is_none_is_refused():
    got = await read_detail(desc_page("@@" + quote("<p>x</p>", safe="")), ext_id="a b/c.1")
    assert got == {"text": "x"}
    with pytest.raises(AdapterError, match="Example Portal posting has no id"):
        await read_detail(desc_page(), ext_id="")


# --- what the reader says of itself, and what a runbook may not say ---------------------------------------------


def test_a_list_asked_through_a_page_first_is_read_as_pages_and_one_that_is_not_is_a_feed():
    assert reader().how == "pages"
    plain = Runbook.model_validate({
        "schema": 1, "key": "example_plain", "name": "Plain", "kind": "job_system", "hosts": list(HOSTS), "order": 92,
        "read": {"kind": "request", "url": "https://feeds.example.test/{slug}", "items": "jobs",
                 "fields": {"ext_id": "id", "title": "title"}}})
    assert adapter_for(plain).how == "feed"


def check(**over) -> feed_schema.Read:
    return feed_schema.check({**READ, **over}, HOSTS)


def test_the_runbook_as_the_tests_write_it_is_a_read():
    assert check().steps[0].capture == {"portal": r"portalNo\s*:\s*'(\d+)'"}


@pytest.mark.parametrize(("over", "why"), [
    ({"steps": [READ["steps"][0]] * 4}, "at most"),
    ({"steps": [{"get": "http://{host}/x", "capture": {"p": "(a)"}}]}, "https"),
    ({"steps": [{"get": "https://{host}/x", "capture": {"p": "a"}}]}, "exactly one group"),
    ({"steps": [{"get": "https://{host}/x", "capture": {"slug": "(a)"}}]}, "not a name for a captured value"),
    ({"steps": [{"get": "https://{host}/x", "capture": {"p": "(a"}}]}, "not a pattern"),
    ({"steps": [{"get": "https://{host}/{later}", "capture": {"p": "(a)"}}]}, "no part of the board"),
    ({"steps": [{"get": "https://{host}/x", "capture": {"p": "(a)"}},
                {"get": "https://{host}/x", "capture": {"p": "(b)"}}]}, "twice"),
    ({"steps": [{"get": "https://example.test/x", "capture": {"p": "(a)"}}], "url": "https://{p}.example.test/x"},
     "host of url"),
    ({"steps": [{"get": "https://{p}.example.test/x", "capture": {"p": "(a)"}}]}, "no part of the board"),
    ({"steps": [{"get": "https://other.test/x", "capture": {"p": "(a)"}}], "url": "https://{host}/{p}"},
     "none of the runbook's hosts"),
    ({"fields": {**READ["fields"], "url": {"template": "https://{host}/{portal}/{ext_id}"}}}, "no part of the board"),
    ({"detail": {**READ["detail"], "url": "https://{host}/view/{portal}/{ext_id}"}}, "no part of the board"),
    ({"kind": "embedded", "blob": "script:x"}, "for a request read"),
    ({"post": {"body": {}, "numbers": {"pageNo": "page"}}, "paging": None}, "go with paging"),
    ({"paging": {**READ["paging"], "total": None}}, "go with the paging's total"),
    ({"unavailable": {"path": "", "says": "x"}}, "empty"),
    ({"detail": {**READ["detail"], "list_pattern": "(a)(b)"}}, "exactly one group"),
    ({"detail": {**READ["detail"], "marker": ""}}, "list_pattern and a marker"),
    ({"detail": {**READ["detail"], "fields": {"text": "t"}}}, "takes no fields"),
    ({"detail": {"url": "https://{host}/v/{ext_id}", "fields": {"text": "t"}, "marker": "@@"}},
     "go with a page_encoded_list"),
    ({"fields": {**READ["fields"], "title": {"cell": {"of": "cols"}}}}, "`at`"),
    ({"fields": {**READ["fields"], "title": {"cell": {"of": "cols", "at": "a", "x": 1}}}}, "an object with"),
    ({"fields": {**READ["fields"], "posted": {"date_cell": {"of": "cols", "not_in": "a"}}}}, "list of fields"),
    ({"fields": {**READ["fields"], "location": {"cells": {"of": "cols", "at": "a", "dashed_places": "yes"}}}},
     "true or false"),
])
def test_a_read_that_says_what_it_may_not_is_refused_in_plain_words(over, why):
    with pytest.raises(ValueError, match=why):
        check(**over)
