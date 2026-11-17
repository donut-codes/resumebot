"""A runbook's `read` of a plain feed (the generic-sites design, Part 1.3; `feed_schema`, `feed_read`): each item of the
grammar is there for a problem a feed has, and each test below puts that problem to a made-up feed
(`feeds.example.test`) behind a mock transport. Nothing here names a real site."""

import json

import httpx
import pytest

from app.jobsearch.adapters import AdapterError
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.postings.index import fault_kind
from app.jobsearch.states import FetchKind
from app.sites import feed_read, feed_schema
from app.sites.schema import Probe, Runbook

HOSTS = ("example.test",)
WHO = "Example Feed"


def read_of(**over) -> dict:
    base = {"kind": "request", "url": "https://feeds.example.test/{slug}/jobs", "items": "jobs",
            "fields": {"ext_id": "id", "title": "title"}}
    return {**base, **over}


def parsed(**over) -> feed_schema.Read:
    return feed_schema.check(read_of(**over), HOSTS)


def fetcher(handle, seen: list[httpx.Request] | None = None) -> Fetcher:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if seen is not None:
            seen.append(request)
        return handle(request)

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0)


def item(read: feed_schema.Read, row: dict, board: dict | None = None):
    return feed_read.parse_item(read, row, board or {"slug": "acme"}, HOSTS)


# --- a field's value ----------------------------------------------------------------------------------------------


def test_the_first_present_of_several_paths_is_the_fields_value_and_a_date_that_is_no_date_is_not_present():
    read = parsed(fields={"ext_id": ["code", "id"], "title": "title", "posted": ["first_on", "updated_on"],
                          "location": ["place.name", "place"]})
    one = item(read, {"id": 7, "title": "T", "first_on": "soon", "updated_on": "2026-10-02T10:00:00-04:00",
                      "place": {"name": "Oslo"}})
    assert (one["ext_id"], one["posted"], one["location"]) == ("7", "2026-10-02", "Oslo")
    two = item(read, {"code": "A1", "id": 7, "title": "T", "first_on": "2026-09-30", "place": "Remote"})
    assert (two["ext_id"], two["posted"], two["location"]) == ("A1", "2026-09-30", "Remote")


def test_several_fields_are_joined_into_a_place_and_a_flag_can_give_a_word_when_the_place_is_empty():
    read = parsed(fields={
        "ext_id": "id", "title": "title",
        "location": [{"join": [["where.city", "alt.city"], "where.state", {"path": "where.country", "upper": True}]},
                     {"text": "Remote", "when": "remote"}]})
    assert item(read, {"id": 1, "title": "T", "where": {"city": "Lyon", "country": "fr"}})["location"] == "Lyon, FR"
    assert item(read, {"id": 1, "title": "T", "alt": {"city": "Bern"}, "where": {"state": "BE"}})["location"] \
        == "Bern, BE"
    assert item(read, {"id": 1, "title": "T", "remote": True})["location"] == "Remote"
    assert item(read, {"id": 1, "title": "T", "remote": False})["location"] is None


def test_a_text_is_made_plain_from_markup_and_a_text_of_several_fields_is_joined():
    read = parsed(fields={"ext_id": "id", "title": "title",
                          "text": [{"join": [{"html": "about"}, {"html": "needs"}], "sep": "\n\n"}]})
    assert item(read, {"id": 1, "title": "T", "about": "<p>One</p>", "needs": "<li>Two</li>"})["text"] \
        == "One\n\n- Two"
    assert item(read, {"id": 1, "title": "T"})["text"] is None


def test_an_address_is_built_from_the_boards_part_and_the_postings_id_and_the_apply_address_is_read_apart():
    read = parsed(fields={
        "ext_id": "id", "title": "title", "url": {"template": "https://{slug}.feeds.example.test/jobs/{ext_id}"},
        "apply_url": "apply"})
    one = item(read, {"id": "a/b", "title": "T", "apply": "https://apply.example.test/x"}, {"slug": "acme"})
    assert one["url"] == "https://acme.feeds.example.test/jobs/a%2Fb"
    assert one["apply_url"] == "https://apply.example.test/x"
    assert item(read, {"title": "T"})["url"] is None  # no id, no address built from it


# --- which rows are postings ---------------------------------------------------------------------------------------


async def test_a_row_that_says_it_is_not_a_posting_is_left_out_and_a_board_of_such_rows_is_a_changed_shape():
    read = parsed(skip=[{"path": "listed", "equals": False}, {"path": "kind", "equals": "internal"}])

    def handle(request):
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "A", "listed": True}, {"id": 2, "title": "B", "listed": False},
            {"id": 3, "title": "C", "kind": "internal"}, {"id": 4, "title": "D"}]})

    found = await feed_read.read_board(fetcher(handle), read, {"slug": "acme"}, who=WHO, hosts=HOSTS)
    assert [p["ext_id"] for p in found.postings] == ["1", "4"]

    def only_skipped(request):
        return httpx.Response(200, json={"jobs": [{"id": 2, "title": "B", "listed": False}]})

    with pytest.raises(AdapterError, match="changed shape"):
        await feed_read.read_board(fetcher(only_skipped), read, {"slug": "acme"}, who=WHO, hosts=HOSTS)


# --- the way of working ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("row", "first", "last"), [
    ({"kind": "Hybrid", "wfh": True, "place": "Remote"}, "hybrid", "remote"),  # the written word, unless flags go first
    ({"wfh": True}, "remote", "remote"),
    ({"onsite_flag": True, "place": "Remote - US"}, "onsite", "onsite"),  # a flag before the place's words
    ({"place": "Remote - US"}, "remote", "remote"),
    ({}, None, None),
])
def test_the_way_of_working_comes_from_the_written_word_the_flags_and_the_places_own_words(row, first, last):
    flags = [{"path": "wfh", "mode": "remote"}, {"path": "onsite_flag", "mode": "onsite"}]
    for order, want in ((False, first), (True, last)):
        read = parsed(fields={"ext_id": "id", "title": "title", "location": "place"},
                      work_mode={"written": "kind", "flags": flags, "flags_first": order})
        assert item(read, {"id": 1, "title": "T", **row})["work_mode"] == want


def test_flags_asked_first_beat_the_written_word():
    read = parsed(work_mode={"written": "kind", "flags": [{"path": "wfh", "mode": "remote"}], "flags_first": True})
    assert item(read, {"id": 1, "title": "T", "kind": "hybrid", "wfh": True})["work_mode"] == "remote"
    assert item(parsed(work_mode={}), {"id": 1, "title": "T"})["work_mode"] is None


# --- pay -------------------------------------------------------------------------------------------------------------


def test_a_pay_object_is_made_into_pay_text_and_numbers_in_cents_or_whole_units():
    cents = parsed(comp={"range": {"path": "ranges.0", "min": "lo", "max": "hi", "currency": "cur",
                                   "period_is": "year", "unit": 100, "writes_text": True}})
    one = item(cents, {"id": 1, "title": "T", "ranges": [{"lo": 9000000, "hi": 12000000, "cur": "USD"}]})
    assert (one["comp_min"], one["comp_max"], one["comp"]) == (90000, 120000, "$90,000 - $120,000 a year")
    assert item(cents, {"id": 1, "title": "T", "ranges": []})["comp"] is None
    hourly = parsed(comp={"range": {"path": "pay", "min": "from", "max": "to", "currency": "cur", "period": "per",
                                    "writes_text": True},
                          "part_time": {"title": "title", "kind": "type"}})
    row = {"id": 1, "title": "Aide", "pay": {"from": 30, "to": 40, "cur": "USD", "per": "hour"}}
    assert item(hourly, row)["comp_min"] is not None
    assert item(hourly, {**row, "title": "Part time aide"})["comp_min"] is None  # an hour of a part-time role


def test_written_pay_keeps_its_own_words_and_takes_numbers_only_from_the_entry_that_counts():
    read = parsed(comp={"written": ["summary", "alt_summary"], "range": {
        "path": "parts", "pick": {"kind": "Salary"}, "min": "low", "max": "high", "currency": "cur",
        "period": "every", "yearly_only": True, "currencies": ["USD"]}})
    parts = [{"kind": "Bonus", "low": 1, "high": 2, "every": "year"},
             {"kind": "Salary", "low": 20, "high": 30, "every": "hour"},
             {"kind": "Salary", "low": 50000, "high": 60000, "every": "1 year", "cur": "EUR"},
             {"kind": "Salary", "low": 70000, "high": 80000, "every": "year", "cur": "USD"}]
    one = item(read, {"id": 1, "title": "T", "alt_summary": "70k to 80k", "parts": parts})
    assert (one["comp"], one["comp_min"], one["comp_max"]) == ("70k to 80k", 70000, 80000)
    none = item(read, {"id": 1, "title": "T", "summary": "negotiable", "parts": parts[:2]})
    assert (none["comp"], none["comp_min"], none["comp_max"]) == ("negotiable", None, None)


# --- reading a board -------------------------------------------------------------------------------------------------


async def test_a_board_part_is_quoted_in_the_address_and_a_board_without_one_has_no_address():
    seen: list[httpx.Request] = []
    read = parsed()
    found = await feed_read.read_board(
        fetcher(lambda r: httpx.Response(200, json={"jobs": [{"id": 1, "title": "A"}]}), seen), read,
        {"slug": "a b/c"}, who=WHO, hosts=HOSTS)
    assert [p["title"] for p in found.postings] == ["A"] and seen[0].url.raw_path.startswith(b"/a%20b%2Fc/jobs")
    for board in ({}, {"slug": ""}, {"slug": 5}):
        with pytest.raises(AdapterError, match="has no address"):
            await feed_read.read_board(fetcher(lambda r: httpx.Response(500)), read, board, who=WHO, hosts=HOSTS)


async def test_a_host_part_is_a_plain_host_under_the_runbooks_hosts_and_a_named_part_matches_its_pattern():
    read = parsed(url="https://{host}/boards/{guid}", board_parts={"guid": "[0-9a-f]{4}"})
    ok = {"host": "Jobs.Example.Test", "guid": "00af"}
    seen: list[httpx.Request] = []
    await feed_read.read_board(fetcher(lambda r: httpx.Response(200, json={"jobs": []}), seen), read, ok,
                               who=WHO, hosts=HOSTS)
    assert str(seen[0].url) == "https://jobs.example.test/boards/00af"
    for bad in ({**ok, "host": "jobs.elsewhere.test"}, {**ok, "host": "example.test"},
                {**ok, "host": "a b.example.test"}, {**ok, "guid": "../x"}, {"host": "jobs.example.test"}):
        with pytest.raises(AdapterError, match="has no address"):
            await feed_read.read_board(fetcher(lambda r: httpx.Response(500)), read, bad, who=WHO, hosts=HOSTS)


async def test_a_paged_read_goes_on_to_the_total_the_first_answer_states():
    seen: list[httpx.Request] = []

    def handle(request):
        start = int(request.url.params["offset"])
        rows = [{"id": i, "title": f"R{i}"} for i in range(start, min(start + 2, 5))]
        return httpx.Response(200, json={"total": 5, "jobs": rows})

    read = parsed(url="https://feeds.example.test/{slug}/jobs?limit={size}&offset={offset}",
                  paging={"param": "offset", "size": 2, "total": "total"})
    found = await feed_read.read_board(fetcher(handle, seen), read, {"slug": "acme"}, who=WHO, hosts=HOSTS)
    assert [p["ext_id"] for p in found.postings] == ["0", "1", "2", "3", "4"] and not found.truncated
    assert [r.url.params["offset"] for r in seen] == ["0", "2", "4"]
    assert {r.url.params["limit"] for r in seen} == {"2"}


async def test_a_paged_read_by_page_number_and_one_whose_total_is_not_stated_is_the_one_page():
    seen: list[httpx.Request] = []
    by_page = parsed(url="https://feeds.example.test/{slug}/jobs?p={page}",
                     paging={"param": "page", "size": 1, "total": "n"})

    def handle(request):
        page = int(request.url.params["p"])
        return httpx.Response(200, json={"n": 2, "jobs": [{"id": page, "title": f"R{page}"}]})

    found = await feed_read.read_board(fetcher(handle, seen), by_page, {"slug": "a"}, who=WHO, hosts=HOSTS)
    assert [p["ext_id"] for p in found.postings] == ["1", "2"]
    seen.clear()
    unstated = parsed(url="https://feeds.example.test/{slug}/jobs?p={page}", paging={"param": "page", "size": 1})
    found = await feed_read.read_board(fetcher(handle, seen), unstated, {"slug": "a"}, who=WHO, hosts=HOSTS)
    assert [p["ext_id"] for p in found.postings] == ["1"] and len(seen) == 1


async def test_a_paged_read_that_is_given_the_same_page_again_stops_and_says_so():
    read = parsed(url="https://feeds.example.test/{slug}/jobs?o={offset}",
                  paging={"param": "offset", "size": 1, "total": "total"})
    found = await feed_read.read_board(
        fetcher(lambda r: httpx.Response(200, json={"total": 9, "jobs": [{"id": 1, "title": "A"}]})), read,
        {"slug": "a"}, who=WHO, hosts=HOSTS)
    assert found.truncated and found.note and "repeated" in found.note and len(found.postings) == 1


async def test_a_page_that_holds_its_list_as_a_data_block_is_read_as_a_feed():
    block = json.dumps({"Jobs": [{"id": 3, "title": "Third", "ok": True}], "pad": "x" * 300})
    page = f"<html><script>window.boardData = {block}</script>"
    read = parsed(kind="embedded", blob="var:boardData", items="Jobs")
    found = await feed_read.read_board(fetcher(lambda r: httpx.Response(200, text=page)), read, {"slug": "a"},
                                       who=WHO, hosts=HOSTS)
    assert [p["title"] for p in found.postings] == ["Third"]
    with pytest.raises(AdapterError, match="changed shape"):
        await feed_read.read_board(fetcher(lambda r: httpx.Response(200, text="<html></html>")), read,
                                   {"slug": "a"}, who=WHO, hosts=HOSTS)


@pytest.mark.parametrize(("status", "text", "kind"), [
    (404, "", FetchKind.GONE), (410, "", FetchKind.REFUSED), (500, "", FetchKind.NETWORK),
    (429, "", FetchKind.NETWORK), (200, "not json", FetchKind.SHAPE),
    (200, '{"other": []}', FetchKind.SHAPE), (200, '{"jobs": [1, 2]}', FetchKind.SHAPE),
    (200, '{"jobs": [{"id": 1}]}', FetchKind.SHAPE),  # rows were read and none had a title
])
async def test_a_read_that_fails_fails_as_a_job_systems_reader_does_so_it_is_classed_the_same(status, text, kind):
    with pytest.raises(AdapterError) as caught:
        await feed_read.read_board(fetcher(lambda r: httpx.Response(status, text=text)), parsed(), {"slug": "a"},
                                   who=WHO, hosts=HOSTS)
    assert fault_kind(caught.value) == kind
    assert WHO in str(caught.value)


async def test_a_board_with_no_postings_is_an_empty_listing_not_a_failure():
    found = await feed_read.read_board(fetcher(lambda r: httpx.Response(200, json={"jobs": []})), parsed(),
                                       {"slug": "a"}, who=WHO, hosts=HOSTS)
    assert found.postings == [] and not found.truncated


# --- one posting -----------------------------------------------------------------------------------------------------


def detail_of(**detail) -> feed_schema.Read:
    return parsed(detail=detail)


async def test_a_postings_own_answer_gives_the_fields_the_list_left_out_and_only_those_it_has():
    read = detail_of(url="https://feeds.example.test/{slug}/jobs/{ext_id}", at="result.opening", objects=["ad"],
                     fields={"text": {"html": "body"}, "posted": ["first_on", "updated_on"], "apply_url": "apply",
                             "location": ["place.name", "place"]},
                     work_mode={"flags": []}, comp={"written": "pay", "parse_written": True})
    seen: list[httpx.Request] = []
    answer = {"result": {"opening": {"body": "<p>Hi</p>", "updated_on": "2026-10-01", "place": "Remote",
                                     "pay": "$80,000 - $100,000 per year", "apply": "https://apply.example.test/1"}}}
    got = await feed_read.read_detail(fetcher(lambda r: httpx.Response(200, json=answer), seen), read,
                                      {"slug": "acme"}, {"ext_id": "12"}, who=WHO, hosts=HOSTS)
    assert got == {"text": "Hi", "posted": "2026-10-01", "apply_url": "https://apply.example.test/1",
                   "location": "Remote", "work_mode": "remote", "comp": "$80,000 - $100,000 per year",
                   "comp_min": 80000, "comp_max": 100000}
    assert seen[0].url.path == "/acme/jobs/12"


async def test_a_detail_answer_that_is_not_the_object_it_should_be_is_a_changed_shape_and_a_bad_id_is_refused():
    read = detail_of(url="https://feeds.example.test/{slug}/jobs/{ext_id}", at="result", objects=["ad"],
                     fields={"text": "body"})
    for answer in ({"result": "no"}, {}, {"result": {"ad": [1]}}):
        with pytest.raises(AdapterError, match="changed shape"):
            await feed_read.read_detail(fetcher(lambda r, a=answer: httpx.Response(200, json=a)), read,
                                        {"slug": "a"}, {"ext_id": "1"}, who=WHO, hosts=HOSTS)
    with pytest.raises(AdapterError, match="has no id"):
        await feed_read.read_detail(fetcher(lambda r: httpx.Response(200, json={})), read, {"slug": "a"},
                                    {"ext_id": "../x"}, who=WHO, hosts=HOSTS)
    with pytest.raises(AdapterError) as caught:
        await feed_read.read_detail(fetcher(lambda r: httpx.Response(404)), read, {"slug": "a"}, {"ext_id": "1"},
                                    who=WHO, hosts=HOSTS)
    assert fault_kind(caught.value) == FetchKind.GONE


async def test_sections_with_titles_make_the_text_of_a_detail():
    read = detail_of(url="https://feeds.example.test/{slug}/jobs/{ext_id}", objects=["ad.sections"], fields={
        "text": {"blocks": [{"at": "ad.sections.about", "heading": "title", "body": "text"},
                            {"at": "ad.sections.missing", "heading": "title", "body": "text"},
                            {"at": "ad.sections.duties", "body": "text"}]}})
    answer = {"ad": {"sections": {"about": {"title": "About", "text": "<p>Us</p>"}, "duties": {"text": "Do"}}}}
    got = await feed_read.read_detail(fetcher(lambda r: httpx.Response(200, json=answer)), read, {"slug": "a"},
                                      {"ext_id": "1"}, who=WHO, hosts=HOSTS)
    assert got == {"text": "About\nUs\n\nDo"}


async def test_a_feed_with_no_answer_for_one_posting_is_read_whole_and_the_posting_picked_out_by_its_id():
    read = parsed(fields={"ext_id": "id", "title": "title", "posted": "on", "text": "plain"},
                  detail={"from_list": True, "take": ["text", "posted"]})
    answer = {"jobs": [{"id": 1, "title": "A", "on": "2026-10-01", "plain": "One"},
                       {"id": 2, "title": "B", "on": "2026-10-02", "plain": "Two"}]}
    got = await feed_read.read_detail(fetcher(lambda r: httpx.Response(200, json=answer)), read, {"slug": "a"},
                                      {"ext_id": "2"}, who=WHO, hosts=HOSTS)
    assert dict(got) == {"text": "Two", "posted": "2026-10-02"} and got.raw == answer["jobs"][1]  # pyright: ignore
    gone = await feed_read.read_detail(fetcher(lambda r: httpx.Response(200, json=answer)), read, {"slug": "a"},
                                       {"ext_id": "9"}, who=WHO, hosts=HOSTS)
    assert dict(gone) == {} and gone.raw is None  # pyright: ignore


PAGE = """<html><script type="application/ld+json">{"@type": "JobPosting", "title": "T", "datePosted": "2026-09-01",
"description": "<p>Work</p>", "jobLocationType": "TELECOMMUTE",
"jobLocation": [{"address": {"addressLocality": "Oslo", "addressCountry": "NO"}}, {"name": "Remote hub"}],
"baseSalary": {"currency": "USD", "value": {"minValue": 71000, "maxValue": 83000, "unitText": "YEAR"}}}
</script></html>"""


async def test_a_postings_page_gives_its_schema_org_places_and_pay_when_the_list_gave_none_of_them():
    read = detail_of(url="https://feeds.example.test/{slug}/jobs/{ext_id}", answer="page_job_posting")
    got = await feed_read.read_detail(fetcher(lambda r: httpx.Response(200, text=PAGE)), read, {"slug": "a"},
                                      {"ext_id": "5", "location": "2 Locations"}, who=WHO, hosts=HOSTS)
    assert got["location"] == "Oslo, NO; Remote hub" and got["work_mode"] == "remote"
    assert (got["comp_min"], got["comp_max"], got["posted"], got["text"]) == (71000, 83000, "2026-09-01", "Work")
    kept = await feed_read.read_detail(fetcher(lambda r: httpx.Response(200, text=PAGE)), read, {"slug": "a"},
                                       {"ext_id": "5", "location": "Paris"}, who=WHO, hosts=HOSTS)
    assert "location" not in kept
    with pytest.raises(AdapterError, match="changed shape"):
        await feed_read.read_detail(fetcher(lambda r: httpx.Response(200, text="<html></html>")), read,
                                    {"slug": "a"}, {"ext_id": "5"}, who=WHO, hosts=HOSTS)


# --- is there a board by this name ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("answer", "board", "name"), [
    ({"offers": [{"company": {"name": " Acme "}}]}, True, "Acme"),
    ({"offers": [{"company": {}}]}, True, None),
    ({"offers": []}, False, None), ({"offers": "x"}, False, None), ({}, False, None), ([], False, None)])
async def test_the_data_form_of_the_probe_finds_a_board_when_its_list_has_postings_and_names_the_employer(
        answer, board, name):
    spec = Probe(url="https://{slug}.feeds.example.test/api", present="offers", name="offers.0.company.name")
    got = await feed_read.probe(fetcher(lambda r: httpx.Response(200, json=answer)), spec, "acme")
    assert (got is not None) == board
    assert got is None or got.name == name


async def test_a_probe_that_asks_for_a_dict_only_and_one_that_is_refused_or_not_json():
    spec = Probe(url="https://feeds.example.test/{slug}")
    assert await feed_read.probe(fetcher(lambda r: httpx.Response(200, json={"name": "x"})), spec, "a") is not None
    for answer in (httpx.Response(404), httpx.Response(200, text="x"), httpx.Response(200, json=[1])):
        assert await feed_read.probe(fetcher(lambda r, a=answer: a), spec, "a") is None
    assert await feed_read.probe(fetcher(lambda r: httpx.Response(200, json={})), Probe(), "a") is None


# --- the grammar is checked ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("over", [
    {"fields": {"title": "title"}},                                     # no id
    {"fields": {"ext_id": "id", "title": "title", "color": "c"}},       # not a posting field
    {"fields": {"ext_id": "id", "title": {"nope": 1}}},                 # not a value
    {"fields": {"ext_id": "id", "title": "a b!"}},                      # not a path
    {"fields": {"ext_id": "id", "title": {"text": "Remote"}}},          # text with no `when`
    {"fields": {"ext_id": "id", "title": {"template": "{slug}{other}"}}},  # not a part of the board
    {"fields": {"ext_id": "id", "title": []}},
    {"url": "http://feeds.example.test/{slug}"},
    {"url": "https://feeds.elsewhere.test/{slug}"},                     # not the runbook's host
    {"url": "https://feeds.example.test/{nothing}"},
    {"url": "https://feeds.example.test/x?o={offset}"},                 # numbers without paging
    {"url": "https://{a}b.example.test/"},
    {"paging": {"param": "offset", "size": 5}},                         # url holds no {offset}
    {"paging": {"param": "offset", "size": 500}},
    {"kind": "embedded"},                                               # no blob
    {"blob": "var:x"},                                                  # a blob of a request
    {"skip": [{"path": "a", "equals": 1}]},
    {"work_mode": {"flags": [{"path": "a", "mode": "sometimes"}]}},
    {"comp": {}},
    {"comp": {"written": "p", "parse_written": True, "range": {"path": "r"}}},
    {"comp": {"range": {"path": "r"}}},                                 # numbers only, no pay as written
    {"comp": {"written": "p", "range": {"path": "r", "writes_text": True}}},
    {"board_parts": {"slug": "[a-z]+"}},
    {"board_parts": {"guid": "("}},
    {"detail": {"url": "https://feeds.example.test/{slug}/x", "fields": {"text": "t"}}},     # no {ext_id}
    {"detail": {"url": "https://feeds.example.test/{ext_id}", "fields": {"title": "t"}}},    # not a detail field
    {"detail": {"from_list": True}},
    {"detail": {"from_list": True, "take": ["title"]}},
    {"detail": {"url": "https://feeds.example.test/{ext_id}", "answer": "page_job_posting", "fields": {"text": "t"}}},
    {"unknown": 1},
])
def test_a_read_that_is_not_the_grammar_is_refused_by_the_schema(over):
    with pytest.raises(ValueError):
        feed_schema.check({**read_of(), **over}, HOSTS)


def runbook(**over) -> dict:
    return {"schema": 1, "key": "feed_a", "name": "Feed A", "kind": "job_system", "order": 0,
            "hosts": ["example.test"], "read": read_of(), **over}


def test_a_runbooks_read_is_checked_when_it_loads_and_built_once_for_the_runbook():
    book = Runbook.model_validate(runbook())
    assert feed_read.of(book) is feed_read.of(book) and feed_read.of(book) is not None
    with pytest.raises(ValueError, match="read: "):
        Runbook.model_validate(runbook(read=read_of(fields={"title": "t"})))
    other = Runbook.model_validate(runbook(read={"kind": "html"}))
    assert feed_read.of(other) is None and feed_read.of(Runbook.model_validate({**runbook(), "read": None})) is None


def test_a_probe_that_is_data_names_its_board_and_nothing_else():
    book = Runbook.model_validate(runbook(probe={"url": "https://x.example.test/{slug}", "present": "a.b",
                                                 "name": "c"}))
    assert book.probe and book.probe.present == "a.b"
    for probe in ({"present": "a"}, {"url": "http://x.example.test/{slug}"}, {"url": "https://x.example.test/"},
                  {"url": "https://x.example.test/{slug}/{other}"}, {"url": "https://x/{slug}", "name": "a b!"}):
        with pytest.raises(ValueError):
            Runbook.model_validate(runbook(probe=probe))
    assert json.loads(Runbook.model_validate(runbook(probe={})).model_dump_json())["probe"]["url"] is None
