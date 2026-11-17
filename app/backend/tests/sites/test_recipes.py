"""Recording a site's listings and checking a recipe against the recording (`sites/recipes.py`).
A recording here is a made-up site's traffic; no network, no browser."""

import json

import pytest

from app.sites import recipes, skills

PAGE = "https://careers.example.test/jobs"
API = "https://careers.example.test/api/jobs"


def _rows(start: int, n: int) -> list[dict]:
    return [{"reqId": f"R{i}", "name": f"Role {i}", "link": f"/job/R{i}", "city": ["Remote", "Ohio"],
             "opened": f"2026-10-{(i % 27) + 1:02d}", "blurb": "<p>" + "text " * 200 + "</p>"}
            for i in range(start, start + n)]


def _raw(**over) -> dict:
    page_one = {"total": 5, "results": _rows(0, 3)}
    base = {"method": "GET", "url": f"{API}?offset=0&limit=3", "post_data": None, "status": 200,
            "content_type": "application/json; charset=utf-8", "body": json.dumps(page_one)}
    return {**base, **over}


def _capture(*extra: dict) -> dict:
    return recipes.sanitize([_raw(), *extra,
                             _raw(url="https://analytics.example.test/collect", body=json.dumps({"ok": True}))],
                            PAGE)


def _recipe(**over) -> dict:
    base = {"kind": "request", "method": "GET", "url": API, "items": "results",
            "params": {"offset": "{offset}", "limit": "{size}"}, "page": {"param": "offset", "size": 3},
            "fields": {"title": "name", "url": "link", "ext_id": "reqId", "location": "city", "posted": "opened"},
            "transforms": {"location": [{"op": "join", "sep": " / "}]}, "url_prefix": "https://careers.example.test"}
    return {**base, **over}


def test_a_capture_keeps_only_public_json_answers_and_never_a_token_or_session():
    raw = [
        _raw(),
        _raw(url="https://careers.example.test/static/app.js", content_type="text/javascript", body="x"),
        _raw(url="http://localhost/api/jobs"),
        _raw(url=f"{API}?access_token=abc123"),
        _raw(url=f"{API}?q=1", method="POST", post_data=json.dumps({"sessionId": "s", "q": 1})),
        _raw(url=f"{API}?q=2", status=403),
        _raw(url=f"{API}?q=3", method="DELETE"),
        _raw(url=f"{API}?q=4", body="not json"),
    ]
    cap = recipes.sanitize(raw, PAGE)
    assert [r["url"] for r in cap["responses"]] == [f"{API}?offset=0&limit=3"]
    assert sum(cap["dropped"].values()) == 7
    assert "headers" not in json.dumps(cap) and "cookie" not in json.dumps(cap).lower()


def test_a_whole_boards_list_in_one_answer_is_kept_and_a_capture_over_its_size_in_all_is_cut(monkeypatch):
    big = json.dumps({"results": _rows(0, 3), "pad": "x" * 1_500_000})
    assert len(recipes.sanitize([_raw(body=big)], PAGE)["responses"]) == 1
    monkeypatch.setattr(recipes, "CAPTURE_MAX", 2_000_000)
    cut = recipes.sanitize([_raw(body=big), _raw(url=f"{API}?offset=3", body=big)], PAGE)
    assert len(cut["responses"]) == 1 and cut["dropped"] == {"over the size of a capture": 1}


def test_over_the_limit_the_biggest_answers_are_kept_since_a_job_list_is_one(monkeypatch):
    monkeypatch.setattr(recipes, "RESPONSES_MAX", 2)
    small = [_raw(url=f"{API}?n={n}", body=json.dumps({"n": n})) for n in range(3)]
    listing = _raw(url=f"{API}?offset=0&limit=50", body=json.dumps({"results": _rows(0, 50)}))
    cap = recipes.sanitize([*small, listing], PAGE)
    assert len(cap["responses"]) == 2 and any("limit=50" in r["url"] for r in cap["responses"])
    assert cap["dropped"] == {"over the limit": 1}


def test_a_capture_packs_and_hashes_the_same_whatever_the_key_order():
    cap = _capture()
    assert recipes.unpack(recipes.pack(cap)) == cap
    assert recipes.fingerprint(cap) == recipes.fingerprint(json.loads(json.dumps(cap, sort_keys=True)))


def test_what_the_model_is_given_shows_the_lists_cut_short_and_never_a_whole_description():
    text = recipes.trim(_capture())
    assert "results" in text and '"count": 3' in text and "characters]" in text
    assert "text " * 100 not in text
    assert len(recipes.trim(_capture(), limit=300)) < 600 and "left out for length" in recipes.trim(_capture(), 300)


async def test_a_good_recipe_reproduces_the_recorded_list_with_the_pages_it_never_loaded_left_empty():
    verdict = await recipes.verify_offline(_recipe(), _capture())
    assert verdict.ok, verdict.problems
    assert (verdict.rows, verdict.recorded) == (3, 3) and verdict.stats["posted"] == 1.0


async def test_a_recipe_for_another_list_path_is_refused_with_what_was_wrong():
    verdict = await recipes.verify_offline(_recipe(items="data.jobs"), _capture())
    assert not verdict.ok and any("changed shape" in p or "no list" in p for p in verdict.problems)


async def test_a_recipe_that_leaves_the_address_out_does_not_reproduce_the_rows():
    lacking = _recipe(fields={"title": "name", "url": "nope", "ext_id": "reqId"}, transforms=None)
    verdict = await recipes.verify_offline(lacking, _capture())
    assert not verdict.ok and any(p.startswith("url is filled") for p in verdict.problems)


async def test_an_endpoint_the_page_never_called_is_an_error_not_a_guess():
    verdict = await recipes.verify_offline(_recipe(url="https://careers.example.test/api/other"), _capture())
    assert not verdict.ok and any("no answer for" in p for p in verdict.problems)


async def test_a_detail_request_the_page_never_made_is_refused():
    bad = _recipe(detail={"url": "https://careers.example.test/api/jobs/{ext_id}", "description": "body"})
    verdict = await recipes.verify_offline(bad, _capture())
    assert not verdict.ok and any("never recorded" in p for p in verdict.problems)


async def test_a_detail_request_the_page_did_make_is_allowed():
    detail = _raw(url="https://careers.example.test/api/jobs/R0", body=json.dumps({"body": "<p>whole</p>"}))
    good = _recipe(detail={"url": "https://careers.example.test/api/jobs/{ext_id}", "description": "body"})
    verdict = await recipes.verify_offline(good, recipes.sanitize([_raw(), detail], PAGE))
    assert verdict.ok, verdict.problems


async def test_a_recipe_that_is_not_valid_says_why_and_does_not_raise():
    verdict = await recipes.verify_offline({"kind": "request", "headers": {"x": "y"}}, _capture())
    assert not verdict.ok and "not valid" in verdict.problems[0]


@pytest.mark.parametrize("history, rows, error, broken", [
    ([], 0, None, "it read no postings"),
    ([{"rows": 100}] * 5, 30, None, "median"),
    ([{"rows": 100}] * 5, 80, None, None),
    ([{"rows": 100}] * 2, 10, None, None),
    ([], 12, "answered 500", "answered 500"),
])
def test_a_night_that_looks_broken_is_judged_so_and_a_normal_one_is_not(history, rows, error, broken):
    why = recipes.judge_night(history, rows, None, error)
    assert (broken in why) if broken else why is None


def test_a_field_that_stops_being_filled_in_looks_broken():
    assert "title" in recipes.judge_night([], 50, {"title": 0.2, "url": 1.0, "ext_id": 1.0}, None)


def test_a_block_is_a_stop_and_not_a_breakage():
    from app.jobsearch.fetch.polite import Refused

    assert recipes.blocked(Refused("robots")) and recipes.blocked(skills.SkillError("x", 429))
    assert not recipes.blocked(skills.SkillError("x", 500))


def _post_raw(frm: int, filtered: bool) -> dict:
    sent = {"lang": "en_xx", "ddoKey": "refineSearch", "from": frm, "size": 3,
            "selected_fields": {"category": ["Ops"]} if filtered else {}}
    return _raw(method="POST", url="https://careers.example.test/widgets", post_data=json.dumps(sent),
                body=json.dumps({"refineSearch": {"data": {"jobs": _rows(frm, 3)}}}))


def _post_recipe(**over) -> dict:
    sent = {"lang": "en_xx", "ddoKey": "refineSearch", "from": "{offset}", "size": "{size}", "selected_fields": {}}
    base = _recipe(method="POST", url="https://careers.example.test/widgets", params=None, body=sent,
                   page={"param": "offset", "size": 3, "in": "body"}, items="refineSearch.data.jobs")
    return {**base, **over}


async def test_a_recipe_may_leave_out_the_filter_the_visitor_applied_when_the_page_asked_for_its_first_page():
    other = _raw(method="POST", url="https://careers.example.test/widgets", post_data='{"ddoKey": "config"}',
                 body=json.dumps({"config": {"x": 1}}))
    capture = recipes.sanitize([other, _post_raw(0, filtered=True)], PAGE)
    verdict = await recipes.verify_offline(_post_recipe(), capture)
    assert verdict.ok, verdict.problems
    assert (verdict.rows, verdict.recorded) == (3, 3)


async def test_a_recipe_that_differs_from_the_recorded_request_in_a_value_that_matters_is_not_reproduced():
    capture = recipes.sanitize([_post_raw(0, filtered=True)], PAGE)
    body = {"lang": "en_yy", "ddoKey": "refineSearch", "from": "{offset}", "size": "{size}", "selected_fields": {}}
    verdict = await recipes.verify_offline(_post_recipe(body=body), capture)
    assert not verdict.ok


def test_a_detail_address_with_the_posting_id_in_its_query_matches_the_recorded_request():
    """Found 2026-10-27 recording two hosts of one job system: `finder=ById;Id="{ext_id}",siteNumber=...` never matched
    the address the page called, so no posting recipe could be kept."""
    template = ('https://jobs.example.test/api/details?expand=all&onlyData=true'
                '&finder=ById;Id="{ext_id}",siteNumber=CX_1')
    recorded = ('https://jobs.example.test/api/details?expand=all&onlyData=true'
                '&finder=ById;Id="18373",siteNumber=CX_1')
    assert recipes._detail_pattern(template).fullmatch(recipes._norm_url(recorded))
    assert not recipes._detail_pattern(template).fullmatch(
        recipes._norm_url(recorded.replace("CX_1", "CX_2")))
