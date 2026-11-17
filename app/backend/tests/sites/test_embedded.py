"""Results data embedded in a page's HTML: found, named, kept only as a fallback, read by a recipe of the kind
`embedded` and checked against the recording (`sites/embedded.py`, `sites/recipes.py`). Made-up pages; no network."""

import json

import pytest

from app.sites import embedded, recipes, skills

PAGE = "https://careers.example.test/search"


def _jobs(start: int, n: int) -> list[dict]:
    return [{"jobId": f"J{i}", "title": f"Role {i}", "path": f"/job/J{i}", "city": "Remote",
             "posted": f"2026-10-{(i % 27) + 1:02d}"} for i in range(start, start + n)]


def _state(start: int = 0, n: int = 4) -> str:
    return json.dumps({"search": {"data": {"jobs": _jobs(start, n), "total": 12}}, "lang": "en"})


def _page_var(start: int = 0) -> str:
    return f"<html><script>var x = 1; appState = {_state(start)};</script></html>"


def _page_script(start: int = 0) -> str:
    return f'<html><script id="__DATA__" type="application/json">{_state(start)}</script></html>'


def _page_ld() -> str:
    return ('<script type="application/ld+json">{"a": 1}</script>'
            f'<script type="application/ld+json">{_state()}</script>')


@pytest.mark.parametrize("html,name", [
    (_page_var(), "var:appState"), (_page_script(), "script:__DATA__"), (_page_ld(), "ldjson:1")])
def test_a_structured_block_is_found_named_and_read_back(html, name):
    found = embedded.find_blobs(html)
    assert [b["blob"] for b in found] == [name]
    assert embedded.extract(html, name) == json.loads(found[0]["body"])
    assert embedded.extract(embedded.wrap(name, found[0]["body"]), name) == json.loads(found[0]["body"])


def test_a_block_that_is_not_a_list_of_postings_is_not_kept():
    flat = ('<script id="cfg" type="application/json">'
            + json.dumps({"theme": "dark", "flags": [1, 2, 3], "x": "y" * 300}) + "</script>")
    assert embedded.find_blobs(flat) == []
    assert embedded.find_blobs("<script>cfg = {not json};</script>") == []


def test_a_block_a_page_no_longer_has_reads_as_none():
    assert embedded.extract("<html></html>", "script:__DATA__") is None


def test_a_block_with_a_closing_script_inside_a_text_survives_the_replay_page():
    body = json.dumps({"jobs": [{"t": "a</script>b"}, {"t": "c"}]})
    assert embedded.extract(embedded.wrap("script:x", body), "script:x") == json.loads(body)


def _blobs(*pages: tuple[str, str]) -> list[dict]:
    return [{"url": u, **b} for u, html in pages for b in embedded.find_blobs(html)]


def _recipe(**over) -> dict:
    base = {"kind": "embedded", "method": "GET", "url": PAGE, "blob": "var:appState", "items": "search.data.jobs",
            "params": {"from": "{offset}"}, "page": {"param": "offset", "size": 4},
            "fields": {"title": "title", "url": "path", "ext_id": "jobId", "location": "city", "posted": "posted"},
            "url_prefix": "https://careers.example.test"}
    return {**base, **over}


def test_the_embedded_data_is_kept_only_when_no_data_request_carried_the_listing():
    blobs = _blobs((PAGE, _page_var()))
    assert len(recipes.sanitize(blobs, PAGE)["embedded"]) == 1
    api = {"method": "GET", "url": "https://careers.example.test/api/jobs", "post_data": None, "status": 200,
           "content_type": "application/json", "body": json.dumps({"results": _jobs(0, 5)})}
    assert "embedded" not in recipes.sanitize([api, *blobs], PAGE)


def test_a_block_with_a_token_or_an_address_that_is_not_public_is_dropped():
    secret = {"url": PAGE, "blob": "var:s", "body": json.dumps({"jobs": _jobs(0, 3), "k": "sk-ant-api03-" + "a" * 40})}
    private = {"url": "http://192.168.0.5/search", "blob": "var:p", "body": _state()}
    got = recipes.sanitize([secret, private], PAGE)
    assert "embedded" not in got or got["embedded"] == []
    assert got["dropped"]


def test_what_the_model_is_given_names_the_block_and_the_lists_in_it():
    text = recipes.trim(recipes.sanitize(_blobs((PAGE, _page_var())), PAGE))
    assert "embedded_in_page" in text and "var:appState" in text and "search.data.jobs" in text


async def test_a_recipe_that_reads_the_block_reproduces_the_recorded_rows_and_pages_not_recorded_end_the_read():
    capture = recipes.sanitize(_blobs((PAGE, _page_var(0))), PAGE)
    verdict = await recipes.verify_offline(_recipe(), capture)
    assert verdict.ok, verdict.problems
    assert (verdict.rows, verdict.recorded) == (4, 4)


async def test_a_second_recorded_page_is_read_by_the_paging():
    capture = recipes.sanitize(_blobs((f"{PAGE}?from=0", _page_var(0)), (f"{PAGE}?from=4", _page_var(4))), PAGE)
    verdict = await recipes.verify_offline(_recipe(), capture)
    assert verdict.ok, verdict.problems
    assert (verdict.rows, verdict.recorded) == (8, 8)


async def test_a_recipe_for_a_block_the_page_never_held_is_refused_with_what_was_wrong():
    capture = recipes.sanitize(_blobs((PAGE, _page_var())), PAGE)
    verdict = await recipes.verify_offline(_recipe(blob="var:other"), capture)
    assert not verdict.ok


def test_an_embedded_recipe_reads_the_page_only():
    skills.parse_recipe(_recipe())
    for bad in (_recipe(blob=None), _recipe(blob="not a name"), _recipe(method="POST", body={"a": 1}),
                _recipe(page={"param": "cursor", "size": 4, "cursor_path": "next"}),
                {**_recipe(), "kind": "request"}):
        with pytest.raises(ValueError):
            skills.parse_recipe(bad)


def test_a_list_of_clips_is_not_the_listing_and_a_list_of_postings_is():
    clips = [{"permalink": f"P{i}", "status": "published", "recording": {"title": None}} for i in range(5)]
    body = lambda rows: {"body": json.dumps({"data": rows})}  # noqa: E731
    assert not recipes.has_listing([body(clips)])
    assert recipes.has_listing([body(_jobs(0, 4))])
