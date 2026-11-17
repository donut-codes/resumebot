"""Prioritization only adds points (docs/PRD.md 30.10, the owner, 2026-10-27): the API refuses a line with negative
points, lets a line the owner already saved that way stay as it is, and scoring gives such a line nothing. Made-up
lines."""

import copy

from app import db
from app.jobsearch.fit import scoring
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import store as profile_store
from tests.support import OWNER, PROFILE, load_profile

SCORING = "/api/v1/profile/scoring"


def _ask(ident: str, points: int) -> dict:
    return {"id": ident, "section": "company", "says": f"Made-up line {ident}", "points": points, "checked_by": "app",
            "fact": "ownership", "op": "in", "value": "public"}


async def _put(client, data: dict):
    return await client.put(SCORING, json={"data": data, "why": "made up"})


async def test_the_api_refuses_a_negative_ask_naming_the_line(js, client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
    refused = await _put(client, {"asks": [_ask("fine", 2), _ask("drag", -1)]})
    assert refused.status_code == 422 and "drag" in refused.json()["detail"] and "fine" not in refused.json()["detail"]
    assert (await _put(client, {"asks": [_ask("fine", 2), _ask("none", 0)]})).status_code == 200


async def test_the_api_refuses_a_negative_statement_and_a_negative_any_of_row(js, client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
    first = {"id": "row", "kind": "best", "points": 0, "section": "company",
             "members": [_ask("up", 2), _ask("down", -1)]}
    refused = await _put(client, {"groups": [first]})
    assert refused.status_code == 422 and "down" in refused.json()["detail"]
    anyof = {"id": "set", "kind": "any", "points": -2, "section": "company",
             "members": [{**_ask("a", 0)}, {**_ask("b", 0)}]}
    refused = await _put(client, {"groups": [anyof]})
    assert refused.status_code == 422 and "set" in refused.json()["detail"]


async def test_a_negative_line_already_saved_stays_so_other_lines_still_save(js, client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        # As the owner saved it before the rule (the store itself refuses nothing).
        await profile_store.save(session, OWNER, "scoring", {"asks": [_ask("kept", -2), _ask("fine", 1)]}, by="owner",
                                 why="made up")
        await session.commit()
    same = {"asks": [_ask("kept", -2), _ask("fine", 3)]}
    assert (await _put(client, same)).status_code == 200, "the old line is left as it is, another one changes"
    worse = {"asks": [_ask("kept", -5), _ask("fine", 3)]}
    refused = await _put(client, worse)
    assert refused.status_code == 422 and "kept" in refused.json()["detail"], "changing its points is a new line"


async def test_the_pages_restore_refuses_an_old_version_with_negative_points_unless_it_is_in_force_as_it_is(js, client):
    """What a save refuses, a restore of an earlier version refuses too (docs/PRD.md 50.13), in the same words."""
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "scoring", {"asks": [_ask("drag", -2), _ask("fine", 1)]}, by="owner")
        await profile_store.save(session, OWNER, "scoring", {"asks": [_ask("fine", 1)]}, by="owner")
        await session.commit()
    refused = await client.post("/api/v1/profile/scoring/versions/1/restore")
    assert refused.status_code == 422 and "Prioritization only adds points" in refused.json()["detail"]
    assert "drag" in refused.json()["detail"] and "fine" not in refused.json()["detail"]
    async with db.session_factory()() as session:
        assert await profile_store.newest_version(session, OWNER, "scoring") == 2
        await profile_store.save(session, OWNER, "scoring", {"asks": [_ask("drag", -2), _ask("fine", 3)]}, by="owner")
        await session.commit()
    allowed = await client.post("/api/v1/profile/scoring/versions/1/restore")
    assert allowed.status_code == 200 and allowed.json()["version"] == 4, "the same points as in force: let through"


def test_a_met_negative_ask_or_statement_earns_nothing_and_takes_nothing_off():
    data = copy.deepcopy(PROFILE)
    data["gates"] = {}
    data["scoring"] = {
        "asks": [_ask("plus", 3), {**_ask("minus", -2), "fact": "ownership", "value": "public"}],
        "groups": [{"id": "row", "kind": "best", "points": 0, "section": "company", "members": [
            {**_ask("lvl-a", -1), "value": "nonprofit"}, {**_ask("lvl-b", -3), "value": "public"}]}]}
    profile = p.Profile.model_validate(data)
    done = scoring.compute({"answers": []}, profile, {"ownership": "public"})
    assert done is not None
    earned = {r["id"]: r["earned"] for r in done["asks"]}
    assert earned["plus"] == 3 and earned["minus"] == 0 and earned["row"] == 0
    assert done["earned"] == 3 and done["possible"] == 3 and done["percent"] == 100
