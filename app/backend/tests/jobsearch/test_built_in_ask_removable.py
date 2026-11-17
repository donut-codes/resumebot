"""The built-in ask that follows a tab's Responsibilities can be removed like any other, and stays removed
(docs/PRD.md 31.6). A scoring records the built-in asks it has been given (`Scoring.built_ins_given`): one with no
record of it (saved before built-in asks existed, or new) is given it once when it is read, and one that was given it
and lacks it had it removed. Everything here is made up."""

import copy

from app.harness import instructions as harness
from app.jobsearch.fit import scoring
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import scoring as ps
from app.jobsearch.profile import views as pv
from app.jobsearch.profile.views import for_scout

BUILDERS = {"id": "builders", "name": "Builders", "titles": ["Director of Programs"],
            "description": "Builds programs.", "responsibilities": "Plans the year and staffs each program."}
CRITERIA = {"company": "A public agency.", "target_roles": [BUILDERS]}
#: A scoring as it was saved before built-in asks existed: two asks of the owner's own, no built-in ask, no record.
OLD = {"asks": [
    {"id": "all-a", "section": "description", "says": "The work is building programs", "points": 2},
    {"id": "degree", "section": "qualifications", "says": "Asks for a degree", "points": 3},
]}
DERIVED = ps.RESPONSIBILITIES_ID


def removed(doc: dict) -> dict:
    """The document as the page sends it back after the owner chose Remove on the built-in ask."""
    out = copy.deepcopy(doc)
    out["asks"] = [a for a in out["asks"] if not a.get("derived")]
    return out


def profile_of(scoring_doc: dict) -> p.Profile:
    return p.Profile.model_validate({"criteria": copy.deepcopy(CRITERIA), "scoring": scoring_doc})


def said(*answers: tuple[str, str]) -> dict:
    return {"asks": [{"id": ident, "answer": answer, "evidence": f"made-up evidence for {ident}"}
                     for ident, answer in answers]}


# --- the upgrade path: an old scoring is given the ask once ---------------------------------------------------------


def test_a_scoring_with_no_record_of_the_built_in_ask_is_given_it_once_when_it_is_read():
    for doc in ({}, {"asks": []}, copy.deepcopy(OLD)):
        s = ps.Scoring.model_validate(doc)
        assert [a.derived for a in s.asks if a.derived] == ["responsibilities"]
        assert s.asks[-1].derived == "responsibilities", "after the owner's own"
        assert s.built_ins_given == ["responsibilities"], "and the scoring now records that it was given it"
        assert ps.Scoring.model_validate(s.model_dump(mode="json")) == s, "read again it is the same: never added twice"
    assert ps.Scoring().asks[-1].derived == "responsibilities", "a new install starts with it"


def test_a_scoring_that_already_has_the_ask_from_before_the_record_is_kept_is_recorded_as_given():
    """Every scoring stored since built-in asks existed holds the ask and no record: the first read notes it, so the
    owner's Remove that follows is not undone."""
    stored = ps.Scoring.model_validate(copy.deepcopy(OLD)).model_dump(mode="json")
    del stored["built_ins_given"]
    s = ps.Scoring.model_validate(stored)
    assert [a.id for a in s.asks] == ["all-a", "degree", DERIVED] and s.built_ins_given == ["responsibilities"]


def test_an_id_the_owner_uses_is_not_taken_over_when_the_built_in_ask_is_given():
    s = ps.Scoring.model_validate({"asks": [{"id": DERIVED, "says": "My own ask", "points": 1}]})
    assert [a.id for a in s.asks] == [DERIVED, f"{DERIVED}-2"] and s.asks[1].derived == "responsibilities"


# --- removed, it stays removed ---------------------------------------------------------------------------------------


def test_a_removed_built_in_ask_is_not_given_again_by_any_later_read():
    saved = removed(ps.Scoring.model_validate(copy.deepcopy(OLD)).model_dump(mode="json"))
    assert saved["built_ins_given"] == ["responsibilities"], "the page sends back the record it was sent"
    s = ps.Scoring.model_validate(copy.deepcopy(saved))
    assert [a.id for a in s.asks] == ["all-a", "degree"] and s.built_ins_given == ["responsibilities"]
    again = ps.Scoring.model_validate(s.model_dump(mode="json"))
    assert again == s and all(not a.derived for a in again.asks), "read, saved and read again, it is still gone"
    prof = profile_of(saved)
    assert not any(a.derived for a in prof.scoring.asks)


def test_the_record_is_kept_once_per_kind_whatever_is_sent():
    s = ps.Scoring.model_validate({"asks": [], "built_ins_given": ["responsibilities", "responsibilities"]})
    assert s.built_ins_given == ["responsibilities"] and not s.asks


async def test_removing_the_built_in_ask_over_http_persists_across_a_save_and_a_read(client):
    first = await client.put("/api/v1/profile/scoring", json={"data": copy.deepcopy(OLD), "why": "an older scoring"})
    assert first.status_code == 200 and first.json()["saved"] is True
    body = (await client.get("/api/v1/profile")).json()
    assert [a["id"] for a in body["scoring"]["asks"] if a["derived"]] == [DERIVED], "an older scoring is given it"
    assert body["scoring"]["built_ins_given"] == ["responsibilities"]

    # The owner chooses Remove and Save: the page sends the document it was sent, less the ask.
    mine = removed(body["scoring"])
    taken_out = await client.put("/api/v1/profile/scoring", json={
        "data": mine, "why": "dropped the responsibilities ask", "base_version": body["versions"]["scoring"]})
    assert taken_out.status_code == 200 and taken_out.json()["saved"] is True

    now = (await client.get("/api/v1/profile")).json()
    assert [a["id"] for a in now["scoring"]["asks"]] == ["all-a", "degree"], "it is gone on the next read"
    assert DERIVED not in now["says"]["asks"]
    assert now["scoring"]["built_ins_given"] == ["responsibilities"]

    # Saving the page again as it stands changes nothing and does not bring it back.
    same = await client.put("/api/v1/profile/scoring", json={"data": now["scoring"], "why": "no change"})
    assert same.json()["saved"] is False
    assert [a["id"] for a in (await client.get("/api/v1/profile")).json()["scoring"]["asks"]] == ["all-a", "degree"]

    # The history says what the owner did, and none of the app's own bookkeeping.
    history = (await client.get("/api/v1/profile/scoring/history")).json()["versions"]
    assert history[0]["changed"] == [f"removed asks: {DERIVED}"]

    # Bringing back the version that had the ask is the way back: it is an ask again, in the history too.
    back = await client.post(f"/api/v1/profile/scoring/versions/{first.json()['version']}/restore")
    assert back.status_code == 200
    again = (await client.get("/api/v1/profile")).json()["scoring"]["asks"]
    assert [a["id"] for a in again if a["derived"]] == [DERIVED]


# --- everything that reads the ask copes with it being gone ----------------------------------------------------------


def test_the_fit_and_the_most_it_can_earn_are_worked_out_without_it():
    kept = profile_of(ps.Scoring.model_validate(copy.deepcopy(OLD)).model_dump(mode="json"))
    gone = profile_of(removed(kept.scoring.model_dump(mode="json")))
    assert [a.id for a in kept.asks_for("builders")] == ["all-a", "degree", DERIVED]
    assert [a.id for a in gone.asks_for("builders")] == ["all-a", "degree"]
    assert scoring.possible(kept, "builders") == 2 + 3 + ps.RESPONSIBILITIES_POINTS
    assert scoring.possible(gone, "builders") == 2 + 3 and scoring.possible(gone) == 2 + 3
    assert scoring.possible_by_role(gone) == {"builders": 5}
    out = scoring.compute(said(("all-a", "met"), ("degree", "partly")), gone, target_role="builders")
    assert [r["id"] for r in out["asks"]] == ["all-a", "degree"] and out["possible"] == 5 and out["total"] == 2, (
        "a partly met ask earns nothing")


def test_answers_already_kept_for_the_removed_ask_are_set_aside_and_a_new_report_naming_it_is_refused():
    kept = profile_of(ps.Scoring.model_validate(copy.deepcopy(OLD)).model_dump(mode="json"))
    gone = profile_of(removed(kept.scoring.model_dump(mode="json")))
    stored = {"answers": [
        {"id": "all-a", "answer": "met", "evidence": "x", "says": "The work is building programs"},
        {"id": DERIVED, "answer": "met", "evidence": "x", "says": ps.RESPONSIBILITIES_SAYS}]}
    worked = scoring.compute(stored, gone, target_role="builders")
    assert [r["id"] for r in worked["asks"]] == ["all-a", "degree"], "a role scored with the ask is scored again"
    assert worked["earned"] == 2 and worked["possible"] == 5
    assert scoring.problems(said((DERIVED, "met")), gone) == [
        f"fit_breakdown names an ask {DERIVED!r} the owner doesn't have"], "so no brief may suggest the id"
    assert scoring.problems(said((DERIVED, "met")), kept) == []


def test_no_agent_is_sent_the_built_in_ask_once_it_is_removed_and_the_reader_still_has_the_responsibilities():
    kept = profile_of(ps.Scoring.model_validate(copy.deepcopy(OLD)).model_dump(mode="json"))
    gone = profile_of(removed(kept.scoring.model_dump(mode="json")))
    sent_kept = for_scout(kept)
    assert DERIVED in str(sent_kept["scoring"]), "kept, it is sent"
    sent = for_scout(gone)
    asks = {a["id"] for s in sent["scoring"]["sections"] for a in s["asks"]}
    assert asks == {"all-a", "degree"}
    assert DERIVED not in str(sent) and "target role it was matched to" not in str(sent)
    sections = {s["section"] for s in sent["scoring"]["sections"]}
    assert "Responsibilities" not in sections, "a part with no ask is left out"
    assert BUILDERS["responsibilities"] in str(sent["what_to_look_for"]), "the reader still reads what the role does"
    assert DERIVED not in str(pv.scoring_for_agent(gone.scoring, gone.criteria))
    assert DERIVED not in str(pv.scoring_for_agent(gone.scoring)), "and with no criteria to say, as before"


def test_the_scoring_instruction_names_no_ask_the_owner_may_have_removed():
    shared = harness.read("instructions", "jobsearch.scoring")
    assert "responsibilities ask" in shared.lower() and "when the profile lists it" in shared
