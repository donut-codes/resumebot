"""What the analyst is shown and told (the owner's review, 2026-10-14): its view of the master resume leaves the
competencies' ids out, since a requirement cannot cite one (`analysis._master_ids` leaves them out and a citation of an
id the master "doesn't have" refuses the whole submission), and its instructions name the tool that takes its findings
in full. Every name, number and sentence is made up."""

from datetime import date

from app import db
from app.agents import roster
from app.harness import instructions as harness_instructions
from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from tests.builders import findings, save_documents, submit_findings
from tests.support import FIVE, OWNER, POSTING_WORDS, answered, load_profile


async def _waiting() -> Opportunity:
    """A Found role with the analyst, and the made-up master resume (which holds a competency with an id)."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates={"other": []})
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        breakdown = scoring.compute(answered(), await profile_store.current(session, OWNER))
        role = Opportunity(
            owner=OWNER, slug="fabrikam", company="Fabrikam", title="Programs Lead", fit=breakdown["total"],
            origin="scout", match_key=match_key("Fabrikam", "Programs Lead"), status=stages.SECOND_IMPRESSION,
                found=date.today(),
            url="https://jobs.example.test/fabrikam", fit_breakdown=breakdown, description=POSTING_WORDS)
        session.add(role)
        await session.flush()
        await analysis.start(session, OWNER, role)
        await session.commit()
        return role


async def test_the_analysts_view_of_the_resume_hides_the_competency_ids_a_requirement_cannot_cite(js):
    role = await _waiting()
    async with db.session_factory()() as session:
        seen = await analysis.brief_part(session, OWNER, await session.get(Opportunity, role.id), "resume")
    assert seen["competencies"] == [{"area": "Governance", "items": ["Risk Frameworks"]}], "no id to cite"
    assert seen["roles"][0]["bullets"][0]["id"] == "b-risk", "every line a requirement may cite keeps its id"
    assert seen["achievements"][0]["id"] == "a-statewide" and seen["education"][0]["id"] == "ms"
    cites = {"item": "Risk frameworks", "class": "T", "from": ["gov"], "why": "The governance area."}
    reply = await submit_findings(findings(role.id, required=[cites]))
    assert reply["taken"] is False and "'gov'" in reply["reason"], "and citing one is still refused"


def test_the_analyst_is_told_the_full_name_of_the_tool_that_takes_its_findings():
    text = harness_instructions.read("instructions", "jobsearch.analyst")
    assert "mcp__jobsearch__jobs_analysis_submit" in text, "a bare name was called in 4 of 152 sessions"
