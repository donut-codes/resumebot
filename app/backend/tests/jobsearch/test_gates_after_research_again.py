"""Research settles every gate it can (the owner, 2026-10-15; the first step is `test_gates_after_research.py`). A rule
set to follow the analyst's classes decides "at least 20% Proven or Transferable" in code from the research. Every
name, number and sentence is made up."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.gating import role_gates
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import Opportunity
from app.jobsearch.profile import gates as pg
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from tests.builders import findings, save_documents, submit_findings
from tests.support import FIVE, OWNER, POSTING_WORDS, answered, load_profile

NOW = datetime.now(UTC)
TODAY = date.today()
SAYS = "At least 20% of the required qualifications must be Proven or Transferable."
QUALIFICATIONS = {"id": "quals", "kind": "rule", "section": "qualifications", "text": SAYS, "from_classes": "PT",
                  "share": 20}
TRAVEL = {"id": "travel", "kind": "rule", "section": "responsibilities",
          "text": "Required travel must be no more than 25%."}
INSTRUCTIONS = Path(__file__).resolve().parents[3] / "instructions"


def gate(**over) -> pg.Gate:
    return pg.Gate.model_validate({**QUALIFICATIONS, **over})


def reader_unknown(rule: dict = TRAVEL, **more) -> dict:
    """A reader's can't-tell from a summary, as a role keeps it."""
    return {"id": rule["id"], "answer": "unknown", "evidence": "From the summary only.", "says": rule["text"], **more}


def classed(*classes: str) -> dict:
    return {"required": [{"item": f"Item {n}", "class": c, "by": "analyst"} for n, c in enumerate(classes, start=1)]}


# --- "at least 20% Proven or Transferable", decided from the classes ----------------------------------------------


@pytest.mark.parametrize(("classes", "want", "fact"), [
    (["P", "P", "P", "P", "P", "T"], "pass", "6 of 6 required items Proven or Transferable, 100%"),
    (["P", "U", "U", "U", "U"], "pass", "1 of 5 required items Proven or Transferable, 20%"),  # exactly 20% passes
    (["U"] * 5, "fail", "0 of 5 required items Proven or Transferable, 0%"),
    (["P"] * 4 + ["U"] * 17, "fail", "4 of 21 required items Proven or Transferable, 19%"),  # 19.05%: under 20
    (["P"] * 5 + ["U"] * 16, "pass", "5 of 21 required items Proven or Transferable, 23%"),
    ([], "unknown", "no required items classed yet"),
])
def test_the_share_of_proven_and_transferable_is_judged_against_the_rule(classes, want, fact):
    assert scoring.class_gate(gate(), classes) == (want, fact)


async def test_a_researched_role_with_five_proven_and_one_transferable_passes_the_rule_whatever_a_reader_said(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates={"other": [QUALIFICATIONS]})
        profile = await profile_store.current(session, OWNER)
    role = Opportunity(owner=OWNER, slug="x", company="Fabrikam", title="Lead", analysis=classed(*"PPPPPT"),
                       gate_answers=[reader_unknown(QUALIFICATIONS)])
    line = {x["id"]: x for x in role_gates.gate_results(role, profile, TODAY)}["quals"]
    assert (line["result"], line["checked_by"]) == ("pass", "app")
    assert line["fact"] == "6 of 6 required items Proven or Transferable, 100%"


async def test_the_analysts_classes_gate_a_role_out_when_under_the_share_and_let_it_on_when_not(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates={"other": [QUALIFICATIONS]})
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        profile = await profile_store.current(session, OWNER)
        ids = {}
        for company in ("Fabrikam", "Initech"):
            breakdown = scoring.compute(answered(), profile)
            role = Opportunity(owner=OWNER, slug=company.lower(), company=company, title="Programs Lead",
                               fit=breakdown["total"], origin="scout", status=stages.SECOND_IMPRESSION, found=TODAY,
                               match_key=match_key(company, "Programs Lead"), fit_breakdown=breakdown,
                               url=f"https://jobs.example.test/{company.lower()}", description=POSTING_WORDS)
            session.add(role)
            await session.flush()
            await analysis.start(session, OWNER, role)
            ids[company] = role.id
        await session.commit()
    unproven = [{"item": f"I{n}", "class": "U"} for n in range(5)]
    over = await submit_findings(findings(ids["Fabrikam"], required=unproven))
    ok = await submit_findings(findings(ids["Initech"]))  # one Proven of two: 50%
    assert (over["stage"], ok["stage"]) == (stages.GATED_OUT, stages.READY_TO_LAUNCH)


# --- the one-off catch-up --------------------------------------------------------------------------------------------
