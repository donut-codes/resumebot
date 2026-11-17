"""Gates on employer facts, the requirements scoring themselves, and the pay ask worked out from the research (paths 25
and 30): a gate or an ask is checked whenever its facts are known, with no preliminary or final. Everything is made
up."""

import copy
from datetime import date

from app import db
from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.gating.role_gates import gate_results
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.profile import gates as pg
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.timeutil import owner_today
from tests.builders import findings, save_documents
from tests.support import FIVE, OWNER, POSTING_WORDS, PROFILE, answered, load_profile

NEVER_PE = {"id": "never-pe", "kind": "employer_fact", "fact": "ownership", "op": "!=", "value": "pe"}
GAPS = {"id": "gaps", "section": "qualifications", "says": "Each Unproven required item", "points": -1,
        "per_item": True, "limit": -3, "from_classes": "U"}
CLEAN = {"id": "clean", "section": "qualifications", "says": "Every required item Proven", "points": 1,
         "from_classes": "all_P"}


def profile(**parts) -> p.Profile:
    data = copy.deepcopy(PROFILE)
    for kind, value in parts.items():
        data[kind] = value
    return p.Profile.model_validate(data)


def test_facts_compare_including_is_not_and_doesnt_include():
    assert scoring.compare("pe", "!=", "pe") is False and scoring.compare("private", "!=", "pe") is True
    assert scoring.compare("health software", "excludes", "gambling") is True
    assert scoring.compare(300, ">=", "1000") is False and scoring.compare("many", ">", 3) is None


def test_an_employer_fact_gate_waits_for_research_then_decides():
    gate = pg.Gate.model_validate(NEVER_PE)
    assert scoring.employer_gate(gate, {}) == ("unknown", "not researched yet")
    assert scoring.employer_gate(gate, {"ownership": "pe"})[0] == "fail"
    assert scoring.employer_gate(gate, {"ownership": "private"})[0] == "pass"
    assert pg.gate_says(gate) == "The employer's ownership is not pe."


def test_the_requirements_score_themselves_from_the_analysts_classes():
    prof = profile(scoring={"asks": [GAPS, CLEAN]})
    out = scoring.compute({"asks": []}, prof, {}, classes=["U", "U", "P"])
    rows = {a["id"]: a for a in out["asks"]}
    assert (rows["gaps"]["answer"], rows["gaps"]["earned"], rows["gaps"]["by"]) == ("met", 0, "app")
    assert "2 required items" in rows["gaps"]["evidence"] and rows["gaps"]["count"] == 2
    # Edited 2026-10-27 (PRD 30.10): the per-item -1 ask earned -2 for two Unproven items and the total was -2; both 0.
    assert rows["clean"]["answer"] == "not" and out["total"] == 0
    out = scoring.compute({"asks": []}, prof, {}, classes=["P", "P"])
    assert {a["id"]: a["answer"] for a in out["asks"]} == {"gaps": "not", "clean": "met"} and out["total"] == 1
    # No classes yet (preliminary): the app can't decide them. A penalty it can't show counts nothing, and neither does
    # a bonus it can't show: no partial credit (docs/PRD.md 30.9).
    out = scoring.compute({"asks": []}, prof, {})
    assert {a["answer"] for a in out["asks"]} == {"unknown"}
    assert [a["earned"] for a in out["asks"]] == [0, 0] and out["total"] == 0


async def test_research_works_out_the_ask_and_the_pay_gate_is_judged_on_it(js):
    async with db.session_factory()() as session:
        gates = {**PROFILE["gates"], "other": [*PROFILE["gates"]["other"], NEVER_PE]}
        await load_profile(session, OWNER, gates=gates, scoring=FIVE)

        await save_documents(session)
        breakdown = scoring.compute(answered(), await profile_store.current(session, OWNER))
        role = Opportunity(
            description=POSTING_WORDS,
            owner=OWNER, slug="fab", company="Fabrikam", title="Programs Lead", fit=breakdown["total"],
                           origin="scout", match_key="fabrikam|programs lead", status=stages.SECOND_IMPRESSION,
                           found=date.today(), location="Remote, US", move_type="remote", fit_breakdown=breakdown)
        session.add(role)
        await session.flush()
        await analysis.start(session, OWNER, role)
        await session.commit()
        role_id = role.id
    async with db.session_factory()() as session:
        await analysis.submit(session, OWNER, findings(role_id))
        await session.commit()
        role = await session.get(Opportunity, role_id)
        worked = role.analysis["ask"]
        assert worked["ask"] >= 95_000 and worked["band"] == [150000, 180000]
        employer = await session.get(Employer, role.employer_id)
        prof = await profile_store.current(session, OWNER)
        gates = {g["id"]: g for g in gate_results(role, prof, owner_today(), employer)}
        assert gates["compensation"]["fact"].startswith("the estimated ask $")
        assert "decided" not in gates["compensation"] and gates["compensation"]["checked_by"] == "app"
        assert gates["never-pe"]["result"] == "pass" and gates["never-pe"]["fact"] == "ownership private"
