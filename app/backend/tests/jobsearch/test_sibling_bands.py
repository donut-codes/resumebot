"""The employer's other pay bands, boardfive code (`sibling_bands`): a sibling is another researched role of the same
employer that posted a band and has a stored market figure; the analyst is handed them and the analysis keeps them, so
the employer pay stance reads them. Everything is made up."""

from app import db
from app.jobsearch.fit import comp, sibling_bands
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.research import analysis
from tests.builders import analysis_setup, findings, submit_findings
from tests.support import OWNER


def _done(employer_id, title, band, market, at="2026-10-10T00:00:00+00:00", state="done") -> Opportunity:
    return Opportunity(
        owner=OWNER, slug=title.lower().replace(" ", "-"), company="Fabrikam", title=title, origin="scout",
        match_key=match_key("Fabrikam", title), status="second_impression", employer_id=employer_id,
            analysis_state=state,
        analysis={"pay": {"band": band, "market_value": market}, "at": at})


async def _employer(ids) -> Employer:
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()
    await submit_findings(findings(ids["Fabrikam"]))
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        return await session.get(Employer, role.employer_id)


async def test_siblings_are_the_employers_other_researched_roles_with_a_posted_band_and_a_market_figure(js):
    ids = await analysis_setup()
    employer = await _employer(ids)
    async with db.session_factory()() as session:
        session.add_all([
            _done(employer.id, "Older Role", [100000, 120000], 105000, at="2026-10-01T00:00:00+00:00"),
            _done(employer.id, "Newer Role", [130000, 150000], 135000, at="2026-10-12T00:00:00+00:00"),
            _done(employer.id, "One Figure", [90000, 90000], 88000),
            _done(employer.id, "No Market", [100000, 120000], None),
            _done(employer.id, "No Band", None, 100000),
            _done(employer.id, "Not Researched", [100000, 120000], 105000, state="waiting"),
        ])
        await session.commit()
        role = await session.get(Opportunity, ids["Fabrikam"])
        built = await sibling_bands.for_role(session, OWNER, role)
    assert [b["name"] for b in built] == ["Newer Role", "One Figure", "Older Role"], "newest research first"
    assert built[0] == {"name": "Newer Role", "low": 130000, "high": 150000, "market": 135000}


async def test_a_role_with_no_employer_has_no_siblings(js):
    async with db.session_factory()() as session:
        assert await sibling_bands.for_role(session, OWNER, Opportunity(id=None, employer_id=None)) == []


def test_the_analysts_own_siblings_never_replace_the_ones_built_for_the_same_role():
    built = [{"name": "Lead", "low": 1, "high": 2, "market": 3}]
    sent = [{"name": "lead", "low": 9, "high": 9, "market": 9}, {"name": "Other", "low": 4, "high": 5, "market": 6}]
    assert sibling_bands.merge(built, sent) == [built[0], sent[1]]


async def test_the_task_carries_the_bands_and_the_analysis_keeps_them_so_the_stance_counts_them(js):
    ids = await analysis_setup()
    employer = await _employer(ids)
    async with db.session_factory()() as session:
        session.add_all([_done(employer.id, f"Role {n}", [200000, 240000], 200000) for n in range(3)])
        initech = await session.get(Opportunity, ids["Initech"])
        initech.employer_id = employer.id
        await analysis.start(session, OWNER, initech)
        await session.commit()
        task = await analysis.analysis_reply(session, OWNER)
    assert task["role"]["id"] == str(ids["Initech"])
    assert {b["name"] for b in task["sibling_bands"]} == {"Role 0", "Role 1", "Role 2"}
    reply = await submit_findings(findings(ids["Initech"]))
    assert reply["taken"] is True
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Initech"])
    assert len(role.analysis["pay"]["siblings"]) == 3
    ask = role.analysis["ask"]
    assert ask["method_version"] == comp.METHOD_VERSION == 4, "the method is the same: only the inputs are new"
    assert ask["stance"] == 1.1, "three bands midpoint 220000 over market 200000"


async def test_a_role_with_no_researched_siblings_is_sent_none(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Hooli"])
        await analysis.start(session, OWNER, role)
        await session.commit()
        brief = await analysis.brief_part(session, OWNER, role, "task")
    assert brief["sibling_bands"] is None
