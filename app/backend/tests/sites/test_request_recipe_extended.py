"""The `request` recipe's extension: query params and placeholders, cursor
paging, field transforms, a detail request, the recipe's own limits. Every site is made up behind a mock transport."""

import httpx
import pytest

from app.jobsearch.fetch.polite import Fetcher
from app.sites import skills
from app.sites.skills import SkillError, parse_recipe, run_recipe

HOST = "https://jobs.example.test"


def _fetcher(handle, seen: list[httpx.Request]) -> Fetcher:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(request)
        return handle(request)

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0)


def _recipe(**over) -> dict:
    base = {"kind": "request", "method": "GET", "url": f"{HOST}/api/search", "items": "jobs",
            "fields": {"title": "title", "url": "path", "ext_id": "id"}, "url_prefix": HOST}
    return {**base, **over}


def _rows(start: int, n: int) -> list[dict]:
    return [{"id": i, "title": f"Role {i}", "path": f"/job/{i}"} for i in range(start, start + n)]


async def test_placeholders_in_params_are_filled_with_the_pages_offset_and_size():
    seen: list[httpx.Request] = []

    def handle(request):
        start = int(request.url.params["start"])
        return httpx.Response(200, json={"jobs": _rows(start, min(2, 5 - start))})

    recipe = _recipe(params={"start": "{offset}", "num": "{size}", "lang": "en"}, page={"param": "offset", "size": 2})
    listing = await run_recipe(_fetcher(handle, seen), recipe)
    assert [p["title"] for p in listing.postings] == [f"Role {i}" for i in range(5)]
    assert [r.url.params["start"] for r in seen] == ["0", "2", "4"]
    assert {r.url.params["num"] for r in seen} == {"2"} and {r.url.params["lang"] for r in seen} == {"en"}
    assert not listing.truncated


async def test_a_post_body_takes_the_page_number_by_placeholder():
    seen: list[httpx.Request] = []

    def handle(request):
        import json

        page = json.loads(request.content)["page"]
        return httpx.Response(200, json={"jobs": _rows(page * 10, 3 if page < 3 else 1)})

    recipe = _recipe(method="POST", body={"page": "{page}", "filters": {"open": True}},
                     page={"param": "page", "size": 3})
    listing = await run_recipe(_fetcher(handle, seen), recipe)
    assert len(listing.postings) == 3 + 3 + 1 and len(seen) == 3


async def test_cursor_paging_follows_the_answers_cursor_to_the_last_page_and_omits_it_on_the_first():
    seen: list[httpx.Request] = []
    cursors = {None: "b", "b": "c", "c": None}

    def handle(request):
        at = request.url.params.get("after")
        return httpx.Response(200, json={"jobs": _rows(len(seen) * 10, 2), "meta": {"next": cursors[at]}})

    recipe = _recipe(params={"after": "{cursor}"}, page={"param": "cursor", "size": 2, "cursor_path": "meta.next"})
    listing = await run_recipe(_fetcher(handle, seen), recipe)
    assert len(listing.postings) == 6
    assert ["after" in r.url.params for r in seen] == [False, True, True]


async def test_a_cursor_that_repeats_stops_the_read_and_says_so():
    seen: list[httpx.Request] = []

    def handle(request):
        return httpx.Response(200, json={"jobs": _rows(len(seen) * 10, 2), "meta": {"next": "same"}})

    recipe = _recipe(params={"after": "{cursor}"}, page={"param": "cursor", "size": 2, "cursor_path": "meta.next"})
    listing = await run_recipe(_fetcher(handle, seen), recipe)
    assert listing.truncated and "repeated" in (listing.note or "") and len(seen) == 2


async def test_the_recipes_request_limit_stops_the_read_with_a_truncated_listing():
    seen: list[httpx.Request] = []

    def handle(request):
        return httpx.Response(200, json={"jobs": _rows(len(seen) * 10, 2)})

    recipe = _recipe(params={"p": "{page}"}, page={"param": "page", "size": 2}, limits={"requests": 2})
    listing = await run_recipe(_fetcher(handle, seen), recipe)
    assert len(seen) == 2 and listing.truncated and skills.LIMIT_REACHED in (listing.note or "")


async def test_field_transforms_make_the_posting_from_the_rows_own_shapes():
    row = {"id": 7, "title": "Analyst", "slug": "analyst-7", "places": [{"name": "Remote"}, {"name": "Ohio"}],
           "opened": "Oct 03 2026", "pay": {"min": 72000, "max": 88000}, "desc": "<p>Do <b>things</b>.</p>",
           "code": "REQ-4411 / Senior"}
    recipe = _recipe(
        items="", fields={"title": "title", "url": "slug", "ext_id": "code", "location": "places", "posted": "opened",
                          "comp": "pay", "summary": "desc"},
        transforms={"url": [{"op": "template", "text": "/job/{value}"}],
                    "location": [{"op": "join", "sep": " / "}],
                    "posted": [{"op": "date", "format": "%b %d %Y"}],
                    "comp": [{"op": "pay"}],
                    "summary": [{"op": "html_text"}],
                    "ext_id": [{"op": "regex", "pattern": r"(REQ-\d+)", "group": 1}]})
    seen: list[httpx.Request] = []
    listing = await run_recipe(_fetcher(lambda r: httpx.Response(200, json=[row]), seen), recipe)
    p = listing.postings[0]
    assert (p["url"], p["location"], p["posted"], p["ext_id"]) == (
        f"{HOST}/job/analyst-7", "Remote / Ohio", "2026-10-03", "REQ-4411")
    assert (p["comp_min"], p["comp_max"]) == (72000, 88000) and p["text"] == "Do things."


async def test_a_transform_that_finds_nothing_leaves_the_field_empty_never_guessed():
    recipe = _recipe(items="", transforms={"ext_id": [{"op": "regex", "pattern": r"(REQ-\d+)"}]})
    row = {"id": "no-code", "title": "Analyst", "path": "/job/1"}
    listing = await run_recipe(_fetcher(lambda r: httpx.Response(200, json=[row]), []), recipe)
    assert listing.postings[0]["ext_id"] == f"{HOST}/job/1"  # no code found: the posting's address stands in


async def test_a_json_detail_gives_the_description_as_text_and_a_gone_posting_gives_none():
    recipe = parse_recipe(_recipe(detail={"url": f"{HOST}/api/job/{{ext_id}}", "description": "data.body",
                                          "transforms": [{"op": "html_text"}]}))
    seen: list[httpx.Request] = []

    def handle(request):
        if request.url.path.endswith("/9"):
            return httpx.Response(404)
        return httpx.Response(200, json={"data": {"body": "<p>The whole <i>posting</i>.</p>"}})

    fetch = _fetcher(handle, seen)
    got = await skills.read_detail(fetch, recipe, "5")
    assert got.text == "The whole posting." and got.url == f"{HOST}/api/job/5"
    assert (await skills.read_detail(fetch, recipe, "9")).text is None


async def test_an_html_detail_reads_the_pages_job_posting():
    page = ('<html><script type="application/ld+json">{"@type":"JobPosting","title":"Analyst","description":'
            '"<p>Whole text here.</p>"}</script></html>')
    recipe = parse_recipe(_recipe(detail={"url": f"{HOST}/careers/{{ext_id}}", "answer": "html"}))
    got = await skills.read_detail(_fetcher(lambda r: httpx.Response(200, text=page), []), recipe, "3")
    assert got.text and "Whole text here." in got.text


async def test_a_detail_answer_that_is_not_json_is_a_changed_shape():
    recipe = parse_recipe(_recipe(detail={"url": f"{HOST}/api/job/{{ext_id}}", "description": "body"}))
    with pytest.raises(SkillError):
        await skills.read_detail(_fetcher(lambda r: httpx.Response(200, text="<html>"), []), recipe, "1")


@pytest.mark.parametrize("change, why", [
    ({"headers": {"Authorization": "x"}}, "not part of the request recipe"),
    ({"params": {"a": "{token}"}, "page": {"param": "page", "size": 5}}, "not a placeholder"),
    ({"params": {"a": "{page}"}}, "placeholders go with paging"),
    ({"params": {"a": "{cursor}"}, "page": {"param": "page", "size": 5}}, "paging is by page"),
    ({"transforms": {"nope": [{"op": "first"}]}}, "does not read"),
    ({"transforms": {"title": [{"op": "regex", "pattern": r"(a+)+b(c+)+"}]}}, "pattern"),
    ({"transforms": {"title": [{"op": "eval"}]}}, ""),
    ({"transforms": {"title": [{"op": "template", "text": "{value}{other}"}]}}, "braces"),
    ({"page": {"param": "cursor", "size": 5}}, "cursor_path"),
    ({"url": "https://localhost/api"}, "public"),
    ({"detail": {"url": "https://127.0.0.1/{ext_id}", "description": "x"}}, "public"),
    ({"detail": {"url": f"{HOST}/job", "description": "x"}}, "ext_id"),
    ({"limits": {"requests": 0}}, ""),
])
def test_a_recipe_that_could_do_more_than_read_a_public_list_is_refused(change, why):
    with pytest.raises(ValueError) as e:
        parse_recipe(_recipe(**change))
    assert why in str(e.value)


def test_a_recipe_saved_before_the_extension_still_parses_and_round_trips_unchanged():
    old = _recipe(page={"param": "offset", "size": 20})
    dumped = parse_recipe(old).model_dump(mode="json", by_alias=True)
    assert parse_recipe(dumped).model_dump(mode="json", by_alias=True) == dumped
    assert dumped["params"] is None and dumped["detail"] is None
