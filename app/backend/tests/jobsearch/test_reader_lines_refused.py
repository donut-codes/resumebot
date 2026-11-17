"""Settings are only what code checks (docs/PRD.md 30, 46, the owner, 2026-10-26): the API refuses a Hard pass or
Prioritization that holds a line only a reader could judge, accepts every line the app checks, and scoring finishes
with no reader answer. Made-up lines."""

import copy

from app import db
from app.jobsearch.fit import scoring
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import store as profile_store
from tests.support import OWNER, PROFILE, load_profile

GATES = "/api/v1/profile/gates"
SCORING = "/api/v1/profile/scoring"


async def _put(client, url: str, data: dict):
    return await client.put(url, json={"data": data, "why": "made up"})


async def test_the_api_refuses_a_sentence_rule_and_accepts_the_lines_the_app_checks(js, client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
    gates = copy.deepcopy(PROFILE["gates"])
    gates["other"] = [{"id": "words", "kind": "rule", "section": "description", "text": "A sentence a reader judges."}]
    refused = await _put(client, GATES, gates)
    assert refused.status_code == 422 and "words" in refused.json()["detail"]
    gates["other"] = [{"id": "age", "kind": "posting_age", "days": 30, "section": "posting"},
                      {"id": "gaps", "kind": "rule", "section": "qualifications", "text": "Proven share",
                       "from_classes": "PT", "share": 20}]
    assert (await _put(client, GATES, gates)).status_code == 200


async def test_the_api_refuses_a_reader_ask_alone_or_in_a_row(js, client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
    alone = {"asks": [{"id": "team", "section": "description", "says": "Leads a team", "points": 1}]}
    assert (await _put(client, SCORING, alone)).status_code == 422
    row = {"groups": [{"id": "row", "kind": "best", "points": 0, "members": [
        {"id": "a", "section": "posting", "says": "Up", "points": 1},
        {"id": "b", "section": "posting", "says": "Same", "points": 0}]}]}
    refused = await _put(client, SCORING, row)
    assert refused.status_code == 422 and "a, b" in refused.json()["detail"]
    ok = {"asks": [{"id": "own", "section": "company", "says": "Public", "points": 1, "checked_by": "app",
                    "fact": "ownership", "op": "in", "value": "public"}]}
    assert (await _put(client, SCORING, ok)).status_code == 200


async def test_the_pages_restore_refuses_an_old_version_with_a_reader_line_and_stores_nothing(js, client):
    """What a save refuses, a restore of an earlier version refuses too (docs/PRD.md 50.13), in the same words."""
    rule = {"id": "words", "kind": "rule", "section": "description", "text": "A sentence a reader judges."}
    ask = {"id": "team", "section": "description", "says": "Leads a team", "points": 1}
    async with db.session_factory()() as session:
        # As the owner saved them before the rule (the store itself refuses nothing).
        await profile_store.save(session, OWNER, "gates", {"other": [rule]}, by="owner")
        await profile_store.save(session, OWNER, "gates", {"other": []}, by="owner")
        await profile_store.save(session, OWNER, "scoring", {"asks": [ask]}, by="owner")
        await profile_store.save(session, OWNER, "scoring", {"asks": []}, by="owner")
        await session.commit()
    for kind, line in (("gates", "words"), ("scoring", "team")):
        refused = await client.post(f"/api/v1/profile/{kind}/versions/1/restore")
        assert refused.status_code == 422 and f"no reader judges a line: {line}" in refused.json()["detail"]
        assert (await client.get(f"/api/v1/profile/{kind}/history")).json()["versions"][0]["version"] == 2


def test_no_reader_is_asked_a_line_of_the_profile_and_scoring_finishes_with_no_reader_answer():
    data = copy.deepcopy(PROFILE)
    data["gates"] = {"other": [{"id": "words", "kind": "rule", "section": "description", "text": "A sentence."}]}
    data["scoring"] = {"asks": [
        {"id": "team", "section": "description", "says": "Leads a team", "points": 2},
        {"id": "own", "section": "company", "says": "Public", "points": 1, "checked_by": "app", "fact": "ownership",
         "op": "in", "value": "public"}]}
    profile = p.Profile.model_validate(data)
    done = scoring.compute({"answers": []}, profile, {"ownership": "public"})
    assert done is not None and done["total"] == 1, "the app's ask earns; the old reader ask waits for no one"
