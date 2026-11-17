"""A place that says nothing is no place (docs/PRD.md 39.15): when research fills a role's location it takes the place
of an empty one and of a placeholder such as "26 Locations" or "Multiple Locations" (`gates.says_no_place`), as the
board's own placeholder is read as no place; a real place the role holds is never overwritten. Every name, number and
sentence is made up."""

from datetime import date

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from tests.builders import findings, save_documents, submit_findings
from tests.support import FIVE, OWNER, POSTING_WORDS, answered, load_profile

#: Only on site in one town is accepted, so a remote role fails the work location gate.
IN_OAKFIELD = {"in_person": {"on": True, "locations": ["Oakfield"]}}


async def _waiting(*roles: tuple[str, dict]) -> dict[str, object]:
    """The made-up profile with the work location gate above and these Found roles `(company, fields)` with the
    analyst: their ids by company."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates={"other": [], "location": IN_OAKFIELD})
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        profile = await profile_store.current(session, OWNER)
        made = {}
        for company, fields in roles:
            breakdown = scoring.compute(answered(), profile)
            role = Opportunity(description=POSTING_WORDS, 
                owner=OWNER, slug=company.lower(), company=company, title="Programs Lead", fit=breakdown["total"],
                origin="scout", match_key=match_key(company, "Programs Lead"), status=stages.SECOND_IMPRESSION,
                found=date.today(), url=f"https://jobs.example.test/{company.lower()}", fit_breakdown=breakdown,
                **fields)
            session.add(role)
            made[company] = role
        await session.flush()
        for role in made.values():
            await analysis.start(session, OWNER, role)
        await session.commit()
        return {company: role.id for company, role in made.items()}


async def test_a_place_that_says_nothing_is_filled_by_research_as_an_empty_one_is_and_a_real_one_never(js):
    ids = await _waiting(
        ("Fabrikam", {"location": "26 Locations"}), ("Initech", {"location": "Multiple Locations"}),
        ("Globex", {"location": "   "}), ("Hooli", {"location": "Oakfield", "move_type": "onsite"}))
    sent = {"location": "Remote, US", "work_mode": "remote"}
    for company in ("Fabrikam", "Initech", "Globex", "Hooli"):
        await submit_findings(findings(ids[company], **sent))
    async with db.session_factory()() as session:
        roles = {company: await session.get(Opportunity, role_id) for company, role_id in ids.items()}
    for company in ("Fabrikam", "Initech", "Globex"):
        role = roles[company]
        assert (role.location, role.move_type) == ("Remote, US", "remote"), company
        assert role.status == stages.GATED_OUT and role.screened_out_by == "location", company
    hooli = roles["Hooli"]
    assert (hooli.location, hooli.status) == ("Oakfield", stages.READY_TO_LAUNCH), "a real place is never overwritten"
