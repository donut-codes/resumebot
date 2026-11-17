"""A job system that is only a runbook (`FeedAdapter`, `app/jobsearch/adapters/feeds.py`): what its addresses say and
what its reader makes of recorded answers, as golden pairs on made-up boards (`example.test`), and the way every
reader a shipped runbook describes fails. Nothing here names a system: the shipped ones are taken from `ADAPTERS` and
put to answers built from their own `read`."""

import json
from typing import cast

import httpx
import pytest

from app.jobsearch.adapters import ADAPTERS, Adapter, AdapterError, Address, System
from app.jobsearch.adapters.feeds import FeedAdapter, adapter_for
from app.jobsearch.fetch.polite import Fetcher, give_time
from app.sites import embedded, registry
from app.sites.schema import Runbook

GUID = "0a1b2c3d-1111-2222-3333-444455556666"
KEYS = {"ext_id", "title", "location", "work_mode", "comp", "comp_min", "comp_max", "posted", "url", "apply_url",
        "text", "department"}


def runbook(key: str, order: int, **more) -> Runbook:
    return Runbook.model_validate({
        "schema": 1, "key": key, "name": key.replace("_", " ").title(), "kind": "job_system",
        "hosts": ["example.test"], "order": order, **more})


def fetcher(handle, seen: list[httpx.Request] | None = None) -> Fetcher:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if seen is not None:
            seen.append(request)
        return handle(request)

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0)


def of(link: str) -> Address:
    found = Address.of(link)
    assert found is not None
    return found


# --- a feed answered page by page, with a posting's own answer and a probe ----------------------------------------

PAGED = runbook(
    "example_paged", 90, flags={"probe_names": True},
    address={
        "board": [
            {"host": r"jobs\.example\.test", "path": r"(?P<slug>[^/]+)(?:/(?s:.*))?",
             "capture": {"slug": "group:slug"}},
            {"host": r".*\.boards\.example\.test", "customer_host": True, "capture": {"slug": "host_name"}}],
        "one_posting": [
            {"path": r"[^/]+/(?P<ext_id>\d{4,})[^/]*(?:/(?s:.*))?", "capture": {"ext_id": "group:ext_id"}},
            {"path": r"[^/]+/\d[^/]*(?:/(?s:.*))?"},
            {"path": r"(?:(?!view/)[^/]+/)*view/[A-Z]+\d+(?:/(?s:.*))?", "query": {"ref": "any"}}],
        "apply_page": [{"path": r"o/(?P<id>[^/]+)/apply", "capture": {"id": "group:id"},
                        "template": "https://{host}/o/{id}"}],
        "board_url": [{"template": "https://jobs.example.test/{slug}", "needs": ["slug"]}]},
    read={
        "kind": "request", "url": "https://api.example.test/{slug}/roles?size=2&offset={offset}", "items": "roles",
        "paging": {"param": "offset", "size": 2, "total": "count"},
        "fields": {
            "ext_id": "id", "title": "name",
            "location": ["where.label",
                         {"join": ["where.city", "where.region", {"path": "where.country", "upper": True}]}],
            "posted": ["first_on", "updated_on"], "url": {"template": "https://jobs.example.test/{slug}/{ext_id}"},
            "text": {"join": [{"html": "about"}, {"html": "needs"}], "sep": "\n\n"}, "department": "team.0.name"},
        "skip": [{"path": "hidden", "equals": True}],
        "work_mode": {"flags": [{"path": "where.remote", "mode": "remote"},
                                {"path": "where.hybrid", "mode": "hybrid"}]},
        "comp": {"range": {"path": "pay", "min": "lo", "max": "hi", "currency": "cur", "period": "per",
                           "writes_text": True}},
        "detail": {"url": "https://api.example.test/{slug}/roles/{ext_id}", "objects": ["sections"],
                   "fields": {"text": {"blocks": [{"at": "sections.job", "heading": "title", "body": "text"},
                                                  {"at": "sections.you", "heading": "title", "body": "text"}]},
                              "posted": "first_on", "apply_url": "apply"}},
    },
    probe={"url": "https://api.example.test/{slug}/roles?size=1", "present": "roles", "name": "roles.0.employer"})

ROLE = {"id": 41, "name": "Planner", "where": {"label": None, "city": "Oslo", "region": None, "country": "no",
                                                "remote": False, "hybrid": True},
        "first_on": "2026-09-30", "about": "<p>Plan.</p>", "needs": "<ul><li>Care</li></ul>", "team": [{"name": "Ops"}],
        "pay": {"lo": 60000, "hi": 80000, "cur": "USD", "per": "year"}}
OTHER = {"id": 42, "name": "Buyer", "where": {"label": "Remote - NO", "remote": True},
         "updated_on": "2026-10-02T10:00:00Z"}
HIDDEN = {"id": 43, "name": "Hidden", "hidden": True}


def answer_page(request: httpx.Request) -> httpx.Response:
    rows = [ROLE, OTHER, HIDDEN, {**ROLE, "id": 44, "name": "Clerk"}]
    start = int(request.url.params["offset"])
    return httpx.Response(200, json={"count": len(rows), "roles": rows[start:start + 2]})


async def test_a_paged_feed_is_read_to_the_total_it_states_and_each_row_is_the_plain_posting_the_runbook_says():
    seen: list[httpx.Request] = []
    listing = await adapter_for(PAGED).read(fetcher(answer_page, seen), {"slug": "acme"})
    assert [int(r.url.params["offset"]) for r in seen] == [0, 2]
    assert not listing.truncated and listing.note is None
    first, second, third = listing.postings
    assert set(first) == KEYS
    assert (first["ext_id"], first["title"], first["location"], first["work_mode"], first["department"]) == (
        "41", "Planner", "Oslo, NO", "hybrid", "Ops")
    assert (first["comp"], first["comp_min"], first["comp_max"]) == ("$60,000 - $80,000 a year", 60000, 80000)
    assert (first["posted"], first["text"]) == ("2026-09-30", "Plan.\n\n- Care")
    assert first["url"] == "https://jobs.example.test/acme/41"
    assert (second["location"], second["work_mode"], second["posted"]) == ("Remote - NO", "remote", "2026-10-02")
    assert third["ext_id"] == "44", "the row that says it is not a posting is left out"
    assert first.raw == ROLE, "the raw copy is the row as the feed gave it"


async def test_a_paged_feed_whose_page_repeats_or_whose_time_runs_out_says_where_it_stopped_and_is_not_the_board():
    def same(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 40, "roles": [ROLE, OTHER]})

    listing = await adapter_for(PAGED).read(fetcher(same), {"slug": "acme"})
    assert len(listing.postings) == 2 and listing.truncated
    assert listing.note == "stopped at 2 of 40 postings (a page repeated one already read)"

    def late(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["offset"])
        if start == 2:
            fetch.deadline = 0.0  # the time allowed ran out as the second page was answered
        return httpx.Response(200, json={"count": 40, "roles": [{**ROLE, "id": start}, {**ROLE, "id": start + 1}]})

    fetch = give_time(fetcher(late), 3600)
    listing = await adapter_for(PAGED).read(fetch, {"slug": "acme"})
    assert len(listing.postings) == 4 and listing.truncated
    assert listing.note == "stopped at 4 of 40 postings (the time allowed for the read ran out)"


async def test_a_postings_own_answer_gives_its_text_date_and_apply_address_and_a_bad_answer_is_a_changed_shape():
    body = {"first_on": "2026-09-24T10:00:00Z", "apply": "https://apply.example.test/41", "sections": {
        "job": {"title": "Job", "text": "<p>Run it.</p>"}, "you": {"title": "You", "text": "<ul><li>Care</li></ul>"}}}
    seen: list[httpx.Request] = []
    got = await adapter_for(PAGED).detail(fetcher(lambda r: httpx.Response(200, json=body), seen), {"slug": "acme"},
                                          {"ext_id": "41"})
    assert seen[0].url.path == "/acme/roles/41"
    assert got == {"text": "Job\nRun it.\n\nYou\n- Care", "posted": "2026-09-24",
                   "apply_url": "https://apply.example.test/41"}
    for answer in (["nope"], {"sections": ["not", "a", "mapping"]}):
        with pytest.raises(AdapterError, match="changed shape"):
            await adapter_for(PAGED).detail(fetcher(lambda r, a=answer: httpx.Response(200, json=a)),
                                            {"slug": "acme"}, {"ext_id": "41"})
    for bad in (None, "", "41/../x", "41?a=b"):
        with pytest.raises(AdapterError, match="no id"):
            await adapter_for(PAGED).detail(fetcher(lambda r: httpx.Response(200, json=body)), {"slug": "acme"},
                                            {"ext_id": bad})


@pytest.mark.parametrize(("answer", "expected"), [
    ({"roles": [{"employer": "Acme Ltd"}]}, "Acme Ltd"), ({"roles": [{"id": 1}]}, None)])
async def test_a_feed_with_a_board_by_that_name_says_so_and_names_its_employer_when_it_can(answer, expected):
    seen: list[httpx.Request] = []
    found = await adapter_for(PAGED).probe(fetcher(lambda r: httpx.Response(200, json=answer), seen), "acme")
    assert found is not None and found.name == expected and "acme" in str(seen[0].url)


@pytest.mark.parametrize("reply", [
    httpx.Response(200, json={"roles": []}), httpx.Response(200, json={}), httpx.Response(200, json=["x"]),
    httpx.Response(404, json={"roles": [{"employer": "x"}]}), httpx.Response(200, text="<html>"),
    httpx.Response(403)])
async def test_a_feed_with_no_board_by_that_name_or_one_that_refuses_gives_none(reply):
    assert await adapter_for(PAGED).probe(fetcher(lambda r: reply), "nobody") is None


def test_a_reader_has_only_what_its_runbook_gives_it_so_the_apps_questions_of_a_reader_are_answered_from_data():
    paged, page, whole = (type(adapter_for(r)) for r in (PAGED, PAGE, WHOLE))
    assert paged.posting is not Adapter.posting and paged.detail is not Adapter.detail
    assert paged.shaped is not System.shaped and paged.apply_page is not System.apply_page
    assert paged.probe is not Adapter.probe
    assert page.apply_page is System.apply_page and page.probe is Adapter.probe, "no rule, no feed to ask"
    assert whole.posting is Adapter.posting and whole.shaped is System.shaped and whole.probe is Adapter.probe
    assert whole.detail is not Adapter.detail and whole.apply_page is System.apply_page


def test_a_runbook_with_a_feed_says_what_the_app_asks_of_a_reader_without_naming_a_line_of_code():
    reader = adapter_for(PAGED)
    assert (reader.key, reader.name, reader.domains, reader.how) == ("example_paged", "Example Paged",
                                                                      ("example.test",), "feed")
    assert (reader.needs, reader.probe_names, reader.places, reader.detail_is_item) == (("slug",), True, False, False)


def test_a_feeds_addresses_are_the_runbooks_board_rules_the_posting_it_reads_and_the_pages_that_have_the_shape():
    reader = adapter_for(PAGED)
    assert reader.board(of("https://jobs.example.test/Acme/1234")) == {"slug": "Acme"}
    assert reader.board(of("https://globex.boards.example.test/x")) == {"slug": "globex"}
    assert reader.board(of("https://www.boards.example.test/x")) is None, "the vendor's own host is no board"
    assert reader.board(of("https://other.example.org/Acme")) is None
    assert reader.posting(of("https://jobs.example.test/acme/12345-planner"), {}) == (
        "12345", "https://jobs.example.test/acme/12345-planner")
    assert reader.posting(of("https://jobs.example.test/acme/123"), {}) is None, "too short to be read alone"
    assert reader.shaped(of("https://jobs.example.test/acme/123"), False)
    assert reader.shaped(of("https://jobs.example.test/acme/view/AB12?ref=x"), False)
    assert not reader.shaped(of("https://jobs.example.test/acme/view/AB12"), False), "the shape asks for its query"
    assert not reader.shaped(of("https://jobs.example.test/acme"), False)
    assert reader.apply_page(of("https://jobs.example.test/o/planner/apply")) == "https://jobs.example.test/o/planner"
    assert reader.apply_page(of("https://jobs.example.test/o/planner")) is None
    assert reader.board_url({"slug": "acme"}) == "https://jobs.example.test/acme"
    assert reader.board_url({}) is None


# --- a page that holds its list, a board named by a host and an id, and an application form --------------------------

PAGE = runbook(
    "example_page", 91, flags={"needs": ["host", "guid"], "places": True},
    address={
        "board": [
            {"host": r".*careers.*example\.test",
             "path": rf"careers(?:/(?!{GUID}(?:/|$))[^/]+)*/(?P<guid>{GUID})(?:/(?s:.*))?",
             "capture": {"host": "host", "guid": "group:guid"}, "lower": ["guid"]},
            {"host": r".*careers.*example\.test", "path": r"careers(?:/(?s:.*))?", "capture": {"host": "host"}}],
        "one_posting": [
            {"path": r"(?:(?!(?:view|form)/)[^/]+/)*view/(?P<ext_id>\d+)(?:/(?s:.*))?",
             "capture": {"ext_id": "group:ext_id"}},
            {"path": r"(?:(?!(?:view|form)/)[^/]+/)*form/(?P<ext_id>\d+)(?:/(?s:.*))?",
             "capture": {"ext_id": "group:ext_id"}, "form": True}],
        "board_url": [{"template": "https://{host}/careers/{guid}", "needs": ["host", "guid"]}]},
    read={
        "kind": "embedded", "url": "https://{host}/careers/{guid}", "blob": "var:boardData", "items": "Rows",
        "board_parts": {"guid": GUID},
        "fields": {"ext_id": "RowId", "title": "RowTitle",
                   "location": [{"join": ["Place.City", "Place.State"]}, "PlaceName"],
                   "url": {"template": "https://{host}/careers/view/{ext_id}"}},
        "skip": [{"path": "Internal", "equals": True}],
        "work_mode": {"flags": [{"path": "Remote", "mode": "remote"}]},
        "detail": {"url": "https://{host}/careers/view/{ext_id}", "answer": "page_job_posting"}})

BOARD = {"host": "careers.example.test", "guid": GUID}


def page_of(rows: list[dict]) -> str:
    return f"<html><script>var boardData = {json.dumps({'Rows': rows, 'Title': 'x'})};</script></html>"


async def test_a_page_that_holds_its_list_is_read_from_the_page_and_a_row_that_is_internal_is_left_out():
    rows = [{"RowId": 7, "RowTitle": "Planner", "Place": {"City": "Oslo", "State": "OS"}},
            {"RowId": 8, "RowTitle": "Remote", "Remote": True, "PlaceName": "Anywhere"},
            {"RowId": 9, "RowTitle": "Staff", "Internal": True}]
    seen: list[httpx.Request] = []
    listing = await adapter_for(PAGE).read(fetcher(lambda r: httpx.Response(200, text=page_of(rows)), seen), BOARD)
    assert str(seen[0].url) == f"https://careers.example.test/careers/{GUID}"
    a, b = listing.postings
    assert (a["ext_id"], a["location"], a["work_mode"], a["url"]) == (
        "7", "Oslo, OS", None, "https://careers.example.test/careers/view/7")
    assert (b["location"], b["work_mode"]) == ("Anywhere", "remote")
    assert adapter_for(PAGE).how == "pages" and adapter_for(PAGE).places


async def test_a_page_without_its_data_is_a_changed_shape_and_a_board_without_an_address_is_never_asked():
    reader = adapter_for(PAGE)
    for page in ("<html>not found</html>", "<script>var boardData = {broken</script>",
                 '<script>var boardData = {"Title": "x"};</script>'):
        with pytest.raises(AdapterError, match="changed shape"):
            await reader.read(fetcher(lambda r, p=page: httpx.Response(200, text=p)), BOARD)
    seen: list[httpx.Request] = []
    for board in ({"host": "careers.example.test"}, {"host": "careers.example.org", "guid": GUID},
                  {"host": "careers.example.test", "guid": "../x"}, {"guid": GUID}):
        with pytest.raises(AdapterError, match="no address"):
            await reader.read(fetcher(lambda r: httpx.Response(200, text=page_of([])), seen), board)
    assert seen == []


async def test_a_postings_page_is_read_from_the_board_the_host_names_even_when_the_boards_id_is_not_known():
    ld = {"@type": "JobPosting", "datePosted": "2026-09-04T13:34:54-05:00", "description": "<p>Do the work.</p>"}
    page = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    seen: list[httpx.Request] = []
    got = await adapter_for(PAGE).detail(fetcher(lambda r: httpx.Response(200, text=page), seen),
                                         {"host": "careers.example.test"}, {"ext_id": "7", "location": "Oslo"})
    assert seen[0].url.path == "/careers/view/7"
    assert got == {"text": "Do the work.", "posted": "2026-09-04"}
    with pytest.raises(AdapterError, match="changed shape"):
        await adapter_for(PAGE).detail(fetcher(lambda r: httpx.Response(200, text="<html></html>")), BOARD,
                                       {"ext_id": "7"})


def test_a_board_is_named_by_its_host_and_its_id_kept_in_lower_case_and_an_application_form_is_read_not_shaped():
    reader = adapter_for(PAGE)
    assert reader.board(of(f"https://careers.example.test/careers/jobs/All/{GUID.upper()}/x")) == {
        "host": "careers.example.test", "guid": GUID}
    assert reader.board(of("https://careers.example.test/careers/jobs")) == {"host": "careers.example.test"}
    assert reader.board(of(f"https://careers.example.test/home/{GUID}")) is None
    view, form = of("https://careers.example.test/careers/view/7"), of("https://careers.example.test/careers/form/7")
    assert reader.posting(view, {}) == ("7", view.given) and reader.posting(form, {}) == ("7", form.given)
    assert reader.shaped(view, True) and not reader.shaped(form, True), "the form opens the posting, it is not its page"
    assert reader.board_url(BOARD) == f"https://careers.example.test/careers/{GUID}"
    assert reader.board_url({"host": "careers.example.test"}) is None


def test_an_item_needs_only_the_parts_of_the_board_its_own_fields_hold_and_not_those_of_the_lists_address():
    row = {"RowId": 7, "RowTitle": "Planner"}
    got = adapter_for(PAGE).parse_item(row, {"host": "careers.example.test"})  # the board's id is the list's, not this
    assert got is not None and got["url"] == "https://careers.example.test/careers/view/7"
    with pytest.raises(AdapterError, match="no address"):
        adapter_for(PAGE).parse_item(row, {"guid": GUID})
    with pytest.raises(AdapterError, match="no address"):
        adapter_for(PAGED).parse_item(ROLE, {})  # the url of a posting is built from the board's slug


# --- a feed with no answer for one posting ----------------------------------------------------------------------------

WHOLE = runbook(
    "example_whole", 92,
    read={"kind": "request", "url": "https://api.example.test/{slug}/all", "items": "roles",
          "fields": {"ext_id": "id", "title": "name", "text": {"html": "about"}, "posted": "on"},
          "detail": {"from_list": True, "take": ["text", "posted"]}})


async def test_a_feed_with_no_answer_for_one_posting_is_read_whole_and_the_posting_picked_out_of_it():
    rows = [{"id": "a1", "name": "One", "about": "<p>First.</p>", "on": "2026-10-01"},
            {"id": "a3", "name": "Three", "about": "<p>Third.</p>", "on": "2026-09-01"}]
    reader = adapter_for(WHOLE)
    assert reader.detail_is_item
    got = await reader.detail(fetcher(lambda r: httpx.Response(200, json={"roles": rows})), {"slug": "acme"},
                              {"ext_id": "a3"})
    assert got == {"text": "Third.", "posted": "2026-09-01"} and got.raw == rows[1], "the raw copy is its row"
    assert await reader.detail(fetcher(lambda r: httpx.Response(200, json={"roles": rows})), {"slug": "acme"},
                               {"ext_id": "gone"}) == {}


# --- every reader the shipped runbooks describe --------------------------------------------------------------------

FEEDS = sorted(key for key, adapter in ADAPTERS.items() if isinstance(adapter, FeedAdapter))


def board_of(adapter: FeedAdapter) -> dict:
    """A board with every part the reader needs, each as a plain word under the runbook's own domain."""
    return {"slug": "acme", "host": f"x.{adapter.domains[0]}", "guid": GUID, "tenant": "acme", "site": "main",
            "section": "main"}


def answer_of(adapter: FeedAdapter, rows) -> httpx.Response:
    """What the reader's feed answers with `rows` as its list, whatever the path to the list and the kind of read."""
    items = adapter.feed.items
    body: object = rows
    for step in reversed(items.split(".") if items else []):
        body = {step: body}
    if adapter.feed.kind == "embedded":
        return httpx.Response(200, text=embedded.wrap(adapter.feed.blob or "", json.dumps(body)))
    return httpx.Response(200, json=body)


def test_the_shipped_feeds_are_there_and_each_is_in_the_order_its_runbook_asks_it():
    assert FEEDS
    asked = {key: at for at, key in enumerate(registry.order())}
    assert [key for key in ADAPTERS if key in FEEDS] == sorted(FEEDS, key=asked.__getitem__)


@pytest.mark.parametrize("key", FEEDS)
async def test_a_feed_that_answers_with_an_error_or_with_a_shape_it_does_not_read_fails_as_a_reader_does(key):
    adapter = ADAPTERS[key]
    assert isinstance(adapter, FeedAdapter)
    board = board_of(adapter)
    with pytest.raises(AdapterError, match="answered 500") as err:
        await adapter.read(fetcher(lambda r: httpx.Response(500, json={"error": "down"})), board)
    assert err.value.status == 500
    with pytest.raises(AdapterError, match="changed shape"):
        await adapter.read(fetcher(lambda r: httpx.Response(200, json={"unexpected": "shape"})), board)
    with pytest.raises(AdapterError, match="changed shape"):
        await adapter.read(fetcher(lambda r: httpx.Response(200, text="<html>sorry</html>")), board)
    with pytest.raises(AdapterError, match="changed shape"):
        await adapter.read(fetcher(lambda r: answer_of(adapter, [{"foo": 1}, {"bar": 2}])), board)


#: The feeds that answer the same whatever page they ask for. One that goes through a page first (`steps`) is given its
#: pages by the tests that know them: `test_feed_steps.py` (a made-up portal) and `test_adapters.py`.
ONE_ANSWER = [key for key in FEEDS if not cast(FeedAdapter, ADAPTERS[key]).feed.steps]


@pytest.mark.parametrize("key", ONE_ANSWER)
async def test_a_board_with_no_postings_is_an_empty_listing_not_an_error(key):
    adapter = ADAPTERS[key]
    assert isinstance(adapter, FeedAdapter)
    listing = await adapter.read(fetcher(lambda r: answer_of(adapter, [])), board_of(adapter))
    assert listing.postings == [] and not listing.truncated


@pytest.mark.parametrize("key", FEEDS)
async def test_a_reader_that_is_refused_a_part_of_its_board_never_asks(key):
    adapter = ADAPTERS[key]
    assert isinstance(adapter, FeedAdapter)
    seen: list[httpx.Request] = []
    for part in adapter.needs:
        for bad in (None, "", 5) if part == "slug" else (None, "", 5, "../x", "a b"):  # a slug is quoted, not refused
            with pytest.raises(AdapterError, match="no address"):
                await adapter.read(fetcher(lambda r: answer_of(adapter, []), seen), {**board_of(adapter), part: bad})
    assert seen == []


@pytest.mark.parametrize("key", FEEDS)
async def test_a_postings_own_answer_that_fails_fails_as_a_reader_does(key):
    adapter = ADAPTERS[key]
    assert isinstance(adapter, FeedAdapter)
    if adapter.feed.detail is None:
        assert await adapter.detail(fetcher(lambda r: httpx.Response(200, json={})), board_of(adapter),
                                    {"ext_id": "31"}) == {}
        return
    with pytest.raises(AdapterError, match="answered 500"):
        await adapter.detail(fetcher(lambda r: httpx.Response(500, json={})), board_of(adapter), {"ext_id": "31"})
