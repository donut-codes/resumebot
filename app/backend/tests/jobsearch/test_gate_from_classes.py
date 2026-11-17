"""A hard pass that follows the analyst's classes (docs/PRD.md 25.5): a sentence saved with `from_classes` and `share`
is decided by the app after research, from how many of the role's required items are in that class, and is not the
reader's. At or over the share it fails and screens the role out with the sentence as the reason; under it, it passes;
before research it is unknown and stops nothing. Everything is made up: the sentence is generic and the figures are
the test's own."""

import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.gating import role_gates, sentence_gates
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import Opportunity
from app.jobsearch.profile import gates as pg
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile import views as pv
from app.jobsearch.research import analysis
from tests.builders import TODAY, analysis_setup, filtered_postings, findings, report_to_run, submit_findings
from tests.support import OWNER, PROFILE, load_profile

SENTENCE = "Most of the required items are unproven. Verify: the analyst's classes of the required items."
#: A rule that fails a role when 80% or more of its required items are Unproven.
GAPS = {"id": "gaps", "kind": "rule", "section": "qualifications", "text": SENTENCE, "from_classes": "U", "share": 80}


def gate(**over) -> pg.Gate:
    return pg.Gate.model_validate({**GAPS, **over})


def profile(*extra, **gates) -> p.Profile:
    """The made-up profile with `extra` lines added to its sentence gates (and `gates` set on the document)."""
    data = copy.deepcopy(PROFILE)
    data["gates"] = {**data["gates"], "other": [*data["gates"]["other"], *copy.deepcopy(list(extra))], **gates}
    return p.Profile.model_validate(data)


def role_row(*classes, target=None, **more):
    """A role as `gate_results` reads it; `classes` are the analyst's classes of its required items (none: not
    researched)."""
    items = [{"item": f"Item {n}", "class": c} for n, c in enumerate(classes, start=1)]
    found = {"required": items} if items else None
    return SimpleNamespace(**{
        "company": "Northwind", "title": "Lead", "fit": 6, "location": "Oakfield, ST", "move_type": "onsite",
        "comp": "$100,000 - $112,500", "posted": None, "found": None, "analysis": found, "fit_breakdown": None,
        "target_role": target, **more})


def result_of(row, prof=None, gate_id="gaps") -> dict:
    return {x["id"]: x for x in role_gates.gate_results(row, prof or profile(GAPS), TODAY)}[gate_id]


# --- the share ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("classes", "share", "want", "fact"), [
    (["U", "U", "U", "U", "P"], 80, "fail", "4 of 5 required items Unproven, 80%"),  # exactly at the share
    (["U", "U", "U", "U", "U"], 80, "fail", "5 of 5 required items Unproven, 100%"),  # over it
    (["U", "U", "U", "P", "P"], 80, "pass", "3 of 5 required items Unproven, 60%"),  # under it
    (["U", "U", "U", "U", "T"], 81, "pass", "4 of 5 required items Unproven, 80%"),  # one point under
    (["U"] * 7 + ["P"] * 2, 78, "pass", "7 of 9 required items Unproven, 77%"),  # 77.8% is under 78, and says 77
    (["U"] * 7 + ["P"] * 2, 77, "fail", "7 of 9 required items Unproven, 77%"),  # and not under 77
    (["U"], 100, "fail", "1 of 1 required item Unproven, 100%"),
    (["P", "T"], 1, "pass", "0 of 2 required items Unproven, 0%"),  # none in the class: under any share
])
def test_a_rule_fails_at_the_share_or_over_it_and_passes_under(classes, share, want, fact):
    assert scoring.class_gate(gate(share=share), classes) == (want, fact)


def test_the_class_that_counts_is_the_owners_choice():
    classes = ["T", "T", "P", "U"]
    assert scoring.class_gate(gate(from_classes="T", share=50), classes) == (
        "fail", "2 of 4 required items Transferable, 50%")
    assert scoring.class_gate(gate(from_classes="P", share=50), classes) == (
        "pass", "1 of 4 required items Proven, 25%")
    assert scoring.class_gate(gate(from_classes="U", share=25), classes) == (
        "fail", "1 of 4 required items Unproven, 25%")


def test_before_the_required_items_are_classed_the_rule_is_unknown_and_stops_nothing():
    assert scoring.class_gate(gate(), []) == ("unknown", "no required items classed yet")
    assert scoring.class_gate(gate(), [None, "", "X"])[0] == "unknown", "an item with no class is not counted"
    # A role nobody has researched, one whose analysis holds no items (the analyst found none, or only refusals).
    for found in (None, {}, {"refused": 1}, {"required": []}):
        out = result_of(role_row(analysis=found))
        assert (out["result"], out["checked_by"]) == ("unknown", "app")


def test_the_classes_are_the_ones_the_analysis_holds_the_owners_corrections_included():
    row = role_row("U", "U", "U", "U", "P")
    assert result_of(row)["result"] == "fail"
    corrected = {**row.analysis["required"][0], "class": "P", "by": "owner"}  # the owner classed the first item again
    row.analysis["required"][0] = corrected
    assert (result_of(row)["result"], result_of(row)["fact"]) == ("pass", "3 of 5 required items Unproven, 60%")


# --- the rule is the app's, so the reader is not asked it ---------------------------------------------------------


def test_the_reader_is_told_the_app_decides_it_and_is_not_sent_it_as_its_own():
    prof = profile(GAPS)
    assert (pg.who_checks(prof.gates.other[-1]), pg.who_checks(prof.gates.other[0])) == ("app", "reader")
    assert {x["id"]: x["who"] for x in pg.gate_lines(prof.gates)}["gaps"] == "app"
    told = {x["id"]: x["who_checks"] for x in pv.for_scout(prof)["hard_gates"]}
    assert told["gaps"] == pv.WHO_CHECKS["app"] and told["travel"] == pv.WHO_CHECKS["reader"]
    answered_by_the_reader = [i for i, who in told.items() if who == pv.WHO_CHECKS["reader"]]
    assert answered_by_the_reader == ["travel"], "the sentence the reader answers is the other one"


def test_an_answer_the_reader_gives_to_it_anyway_is_kept_and_read_for_nothing():
    prof = profile(GAPS)
    answers = [{"id": "gaps", "answer": "fail", "evidence": "It looks unproven."},
               {"id": "travel", "answer": "pass", "evidence": "No travel."}]
    assert sentence_gates.problems(answers, prof) == [], "not refused: it is the app's gate, not an unknown one"
    # The role page judges it from the classes, whatever the reader said.
    under = result_of(role_row("U", "P", "P", "P", "P", gate_answers=answers))
    assert (under["result"], under["checked_by"]) == ("pass", "app")
    over = result_of(role_row("U", "U", "U", "U", "U", gate_answers=[{**answers[0], "answer": "pass"}]))
    assert over["result"] == "fail"


async def test_a_reader_that_fails_it_anyway_does_not_screen_the_role_out(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=profile(GAPS).gates.model_dump(mode="json"))
        await filtered_postings(session, 1)
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        run_id = started.id
    role = {"company": "Fabrikam", "title": "Reader Role 1", "url": "https://jobs.example.test/reader-role-1",
            "fit_evidence": "Biggest gap: none.",
            "fit_breakdown": {"asks": [{"id": "promotion", "answer": "met", "evidence": "A step up."}]},
            "gate_answers": [{"id": "gaps", "answer": "fail", "evidence": "It looks unproven."},
                             {"id": "travel", "answer": "pass", "evidence": "No travel."}]}
    await report_to_run(run_id, [role])
    await report_to_run(run_id, [], done=True)
    async with db.session_factory()() as session:
        stored = (await session.scalars(select(Opportunity))).all()
    assert [(o.status, o.screened_out_reason) for o in stored] == [(stages.LANDING_ZONE, None)]
    assert [a["id"] for a in stored[0].gate_answers] == ["gaps", "travel"], "its answer is kept all the same"


# --- after research: the settle step and the role page ------------------------------------------------------------


async def _setup(share=80):
    """`analysis_setup`'s three Found roles, with the rule on the owner's gates; returns their ids."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        gates = copy.deepcopy(PROFILE["gates"])
        gates["other"].append({**GAPS, "share": share})
        await profile_store.save(session, OWNER, "gates", gates, by="test")
        await session.commit()
    return ids


async def _research(role_id, classes, **more):
    """The analyst's findings for a role, its required items classed `classes` (`more` adds to the findings); returns
    the app's reply and the role."""
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, role_id))
        await session.commit()
    required = [{"item": f"Item {n}", "class": c, **({"why": "the risk working group", "from": ["b-risk"]}
                                                      if c in ("P", "T") else {})}
                for n, c in enumerate(classes, start=1)]
    reply = await submit_findings(findings(role_id, required=required, **more))
    async with db.session_factory()() as session:
        return reply, await session.get(Opportunity, role_id)


async def test_a_researched_role_with_the_share_or_more_unproven_is_gated_out_with_the_sentence_as_the_reason(js):
    ids = await _setup(80)
    reply, role = await _research(ids["Fabrikam"], ["U", "U", "U", "U", "P"])
    assert reply["stage"] == stages.GATED_OUT, "4 of 5 is 80%: at the share"
    assert role.status == stages.GATED_OUT and role.screened_out_by == "rule"
    assert (role.screened_out_step, role.screened_out_gate_id) == ("gate", "gaps"), "the app decides it, not a reader"
    assert role.screened_out_reason == SENTENCE.rstrip(".") + " (4 of 5 required items Unproven, 80%)."
    assert role.analysis_state == analysis.DONE, "the research is kept whole"


async def test_a_researched_role_under_the_share_goes_on_to_the_drafting_bar(js):
    ids = await _setup(80)
    reply, role = await _research(ids["Fabrikam"], ["U", "U", "U", "P", "P"])
    assert reply["stage"] == stages.READY_TO_LAUNCH, "3 of 5 is 60%, and the fit is 80%, over the drafting bar"
    assert (role.status, role.screened_out_reason) == (stages.READY_TO_LAUNCH, None)


async def test_a_role_whose_analysis_classed_no_required_items_is_not_stopped_by_it(js):
    """A posting with duties and no requirements section: the findings grade one duty and no required item, so the rule
    has no share to judge and stops nothing, set as low as 1%. (Findings that grade nothing at all are no longer taken:
    docs/PRD.md 39.30, `test_graded_without_posting.py`.)"""
    ids = await _setup(1)
    duty = {"item": "Lead the programs", "class": "P", "why": "the risk working group", "from": ["b-risk"]}
    reply, role = await _research(ids["Fabrikam"], [], responsibilities=[duty])
    assert reply["stage"] == stages.READY_TO_LAUNCH and role.screened_out_reason is None
    assert role.analysis["required"] == [] and len(role.analysis["responsibilities"]) == 1


async def test_the_role_page_says_unknown_before_research_and_the_figures_after(client):
    ids = await _setup(80)

    async def shown(role_id):
        body = (await client.get(f"/api/v1/roles/{role_id}")).json()
        return {g["id"]: g for g in body["gates"]}["gaps"]

    before = await shown(ids["Fabrikam"])
    assert (before["result"], before["checked_by"], before["says"]) == ("unknown", "app", SENTENCE)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        assert role.status == stages.SECOND_IMPRESSION, "unknown stops nothing"
    await _research(ids["Fabrikam"], ["U", "U", "U", "U", "U"])
    after = await shown(ids["Fabrikam"])
    assert (after["result"], after["checked_by"]) == ("fail", "app")
    assert after["fact"] == "5 of 5 required items Unproven, 100%"
    await _research(ids["Initech"], ["P", "P", "U", "P", "P"])
    passed = await shown(ids["Initech"])
    assert (passed["result"], passed["fact"]) == ("pass", "1 of 5 required items Unproven, 20%")


def test_a_rule_on_a_roles_own_tab_is_judged_for_that_role_and_a_part_that_is_off_stops_nothing():
    mostly = ["U"] * 5
    own = profile({**GAPS, "roles": ["builders"]})
    assert result_of(role_row(*mostly, target="builders"), own)["result"] == "fail"
    assert "gaps" not in {x["id"] for x in role_gates.gate_results(role_row(*mostly), own, TODAY)}
    assert "gaps" not in {x["id"] for x in role_gates.gate_results(role_row(*mostly, target="analysts"), own, TODAY)}
    off = profile(GAPS, sections_off=["qualifications"])
    assert "gaps" not in {x["id"] for x in role_gates.gate_results(role_row(*mostly), off, TODAY)}


# --- what a save accepts -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("over", [
    {"share": 0}, {"share": 101}, {"share": -5}, {"share": 80.5}, {"share": "most"},
    {"from_classes": "X"}, {"from_classes": "u"}, {"from_classes": "all_P"},  # an ask's all_P is not a class of a gate
    {"share": None}, {"from_classes": None},  # both or neither
    {"kind": "employer_fact", "fact": "ownership", "op": "!=", "value": "pe"}, {"kind": "posting_age", "days": 30},
])
def test_a_save_must_name_a_class_of_the_analyst_and_a_share_from_1_to_100_on_a_rule(over):
    with pytest.raises(ValidationError):
        gate(**over)


def test_every_class_and_every_share_from_1_to_100_is_accepted_and_a_blank_is_none():
    for share in (1, 50, 80, 100):
        assert gate(share=share).follows_classes() is True
    for cls in ("U", "T", "P"):
        assert gate(from_classes=cls).follows_classes() is True
    blank = gate(from_classes="", share="")
    assert (blank.from_classes, blank.share, blank.follows_classes()) == (None, None, False)
    plain = pg.Gate.model_validate({"id": "x", "kind": "rule", "text": "A rule."})
    assert (plain.from_classes, plain.share, plain.follows_classes()) == (None, None, False), "no rule has it by itself"
    again = pg.GateSet.model_validate(profile(GAPS).gates.model_dump(mode="json"))
    assert again == profile(GAPS).gates, "saved as it was read, it loads the same"


async def test_a_bad_save_is_refused_with_the_reason_and_a_good_one_is_read_back(client):
    bad = {**PROFILE["gates"], "other": [{**GAPS, "share": 150}]}
    refused = await client.put("/api/v1/profile/gates", json={"data": bad, "why": "too much"})
    assert refused.status_code == 422
    assert "don't fit" in refused.json()["detail"] and "share" in refused.json()["detail"]
    half = {**PROFILE["gates"], "other": [{k: v for k, v in GAPS.items() if k != "share"}]}
    unfinished = await client.put("/api/v1/profile/gates", json={"data": half, "why": "no share"})
    assert unfinished.status_code == 422 and "needs both from_classes" in unfinished.json()["detail"]
    good = {**PROFILE["gates"], "other": [*PROFILE["gates"]["other"], GAPS]}
    assert (await client.put("/api/v1/profile/gates", json={"data": good, "why": "most unproven"})).status_code == 200
    body = (await client.get("/api/v1/profile")).json()
    mine = {g["id"]: g for g in body["gates"]["other"]}
    assert (mine["gaps"]["from_classes"], mine["gaps"]["share"]) == ("U", 80)
    assert (mine["travel"]["from_classes"], mine["travel"]["share"]) == (None, None)
    assert body["says"]["gates"]["other"]["gaps"] == SENTENCE, "the page shows the sentence as it was written"
