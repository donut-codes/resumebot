"""The app never creates, changes or removes a row, an ask, a suggestion or the graded requirements on its own
(the prioritization groups spec, rule 11). Loading, saving, restoring and re-scoring leave what the owner
wrote as it was; only the owner's own save changes it. Made-up asks only."""

import copy

from app.jobsearch.fit import scoring
from app.jobsearch.profile import schema as p
from tests.support import GRADED_ON

ASKS = [{"id": "ask-a", "section": "posting", "says": "A promotion", "points": 1},
        {"id": "ask-b", "section": "qualifications", "says": "Every required item is proven", "points": 2},
        {"id": "ask-c", "section": "preferred", "says": "Preferred items are proven", "points": 1}]
REMOVE = {"id": "sug-r", "kind": "remove", "asks": ["ask-b", "ask-c"], "points": 0,
          "why": "Grading the requirements counts these already."}
GROUP = {"id": "sug-g", "kind": "any", "asks": ["ask-a", "ask-b"], "points": 1, "why": "Either one."}


def stored(read: dict) -> dict:
    """The part of a read profile's scoring that is the owner's: the document a second save would send."""
    return {k: read[k] for k in ("asks", "groups", "suggestions", "grade_requirements", "sections_off")}


async def test_a_document_with_no_rows_stays_with_none_through_a_save_a_read_a_restore_and_the_rescore(client):
    saved = await client.put("/api/v1/profile/scoring", json={"data": {"asks": ASKS}, "why": "plain asks"})
    assert saved.status_code == 200 and saved.json()["saved"] is True
    read = (await client.get("/api/v1/profile")).json()["scoring"]
    assert (read["groups"], read["suggestions"], read["grade_requirements"]) == ([], [], None)
    assert [a["id"] for a in read["asks"] if not a.get("derived")] == ["ask-a", "ask-b", "ask-c"]
    # Saving it again (which works every score out again) and restoring the first version change none of it.
    assert (await client.put("/api/v1/profile/scoring", json={"data": read})).json()["saved"] is False
    assert (await client.post("/api/v1/profile/scoring/versions/1/restore")).status_code == 200
    after = (await client.get("/api/v1/profile")).json()["scoring"]
    assert stored(after) == stored(read)


async def test_ticking_the_graded_requirements_takes_no_ask_out_and_a_stored_suggestion_changes_nothing(client):
    plain = {"asks": ASKS, "suggestions": [REMOVE, GROUP]}
    assert (await client.put("/api/v1/profile/scoring", json={"data": plain})).json()["saved"] is True
    ticked = {**plain, "grade_requirements": GRADED_ON}
    assert (await client.put("/api/v1/profile/scoring", json={"data": ticked, "why": "ticked the box"})).json()["saved"]
    read = (await client.get("/api/v1/profile")).json()["scoring"]
    assert [a["id"] for a in read["asks"] if not a.get("derived")] == ["ask-a", "ask-b", "ask-c"], "no ask was removed"
    assert [s["id"] for s in read["suggestions"]] == ["sug-r", "sug-g"] and read["groups"] == []
    assert read["grade_requirements"] == GRADED_ON


def test_a_suggestion_is_a_note_and_changes_no_score():
    said = {"asks": [{"id": i, "answer": a, "evidence": "made-up"} for i, a in (("ask-a", "met"), ("ask-b", "partly"),
                                                                              ("ask-c", "not"))]}
    with_notes = p.Profile.model_validate({"scoring": {"asks": copy.deepcopy(ASKS), "suggestions": [REMOVE, GROUP]}})
    without = p.Profile.model_validate({"scoring": {"asks": copy.deepcopy(ASKS)}})
    assert scoring.compute(said, with_notes) == scoring.compute(said, without)
    assert scoring.possible(with_notes) == scoring.possible(without)


async def test_only_the_owners_own_save_takes_the_asks_a_removal_names_out_and_the_history_says_so(client):
    plain = {"asks": ASKS, "suggestions": [REMOVE], "grade_requirements": GRADED_ON}
    await client.put("/api/v1/profile/scoring", json={"data": plain})
    # The page's "Remove these asks" followed by Save sends the document less the named asks and the suggestion.
    owners = {"asks": [a for a in ASKS if a["id"] == "ask-a"], "suggestions": [], "grade_requirements": GRADED_ON}
    assert (await client.put("/api/v1/profile/scoring", json={"data": owners, "why": "graded instead"})).json()["saved"]
    read = (await client.get("/api/v1/profile")).json()["scoring"]
    assert [a["id"] for a in read["asks"] if not a.get("derived")] == ["ask-a"] and read["suggestions"] == []
    history = (await client.get("/api/v1/profile/scoring/history")).json()["versions"]
    assert "removed suggestions: sug-r" in history[0]["changed"]
    assert {"removed asks: ask-b", "removed asks: ask-c"} <= set(history[0]["changed"])
