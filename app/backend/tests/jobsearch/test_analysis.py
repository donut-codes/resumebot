"""The analyst (path 23.3): which Found roles it researches (all of them, never twice: no research bar), its task, its
findings filling the employer record and rescoring the role, the gates run again on the facts, the drafting bar moving
the role, refusals, the stuck sweep, the mission, and the runner told what work waits. Everything is made up."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import role_gates
from app.jobsearch.missions import core as missions
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.work import session_kinds
from app.timeutil import owner_today
from tests.builders import analysis_setup, findings, submit_findings
from tests.support import OWNER, POSTING_WORDS, answered


async def test_the_analyst_researches_every_found_role_whatever_its_fit_best_first_with_no_research_bar(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        picked = await analysis.picks(session, OWNER, 5)
        assert [r.company for r in picked] == ["Fabrikam", "Initech", "Hooli"], (
            "Hooli's 40% would have waited under a research bar of 50%; there is none, and an age goes best first")
        for role in picked:
            await analysis.start(session, OWNER, role)
        await session.commit()
        assert await analysis.picks(session, OWNER, 5) == [], "never twice"
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.ANALYSIS
        task = await analysis.analysis_reply(session, OWNER)
        assert task["role"]["id"] == str(ids["Fabrikam"]) and "You are the analyst" in task["instructions"]
        assert "resume" in task["read_next"] and "roles" in (await analysis.analysis_reply(session, OWNER, "resume"))
        seen = await analysis.analysis_reply(session, OWNER, "resume")
        assert "evidence" not in seen and seen["roles"][0]["tools"] == ["Visio", "Miro"]


async def test_an_analyst_that_holds_the_owners_material_in_its_system_prompt_is_not_sent_it_again(js):
    """The resume, scoring, response shape and instructions came back in the analyst's tool replies
    every session, about 17k tokens each, so none of it could be read from Claude's prompt cache. A session that names
    the digest of what the app holds (`X-Context`) is sent the task alone; any other is sent everything, as before."""
    from app.jobsearch.research import session_context

    await analysis_setup()
    async with db.session_factory()() as session:
        role = (await analysis.picks(session, OWNER, 5))[0]
        await analysis.start(session, OWNER, role)
        await session.commit()
        text = await session_context.text(session, OWNER, session_context.ANALYSIS)
        assert "## master_resume" in text and "## scoring" in text and "## response_template" in text
        digest = session_context.digest(text)
        again = await session_context.text(session, OWNER, session_context.ANALYSIS)
        assert text == again, "the same prefix each time"
        held = await analysis.brief_part(session, OWNER, role, "task", digest)
        for left_out in ("scoring", "response_template", "instructions", "read_next"):
            assert left_out not in held, left_out
        assert "master resume" in held["in_your_system_prompt"] and "gates" in held
        stale = await analysis.brief_part(session, OWNER, role, "task", "not-the-digest")
        assert stale["scoring"]["sections"] and stale["response_template"] and stale["read_next"] == ["resume"]
        assert "You are the analyst" in stale["instructions"] and "in_your_system_prompt" not in stale
        everything = await analysis.brief_part(session, OWNER, role, "context", digest)
        assert set(everything) == {"master_resume", "scoring", "response_template"}
        assert everything["scoring"] == stale["scoring"]
        assert everything["response_template"] == stale["response_template"]


async def test_findings_fill_the_employer_record_rescore_and_send_a_strong_role_to_the_writer(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()
    reply = await submit_findings(findings(ids["Fabrikam"]))
    assert reply["taken"] is True and reply["stage"] == stages.READY_TO_LAUNCH, "80% is over the 75% drafting bar"
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        employer = await session.get(Employer, role.employer_id)
        assert (employer.employees, employer.founded, employer.ownership) == (300, 1990, "private")
        assert employer.sources["employees"]["source"] == "https://about.example.test"
        assert role.description == POSTING_WORDS, "the posting's own words stay; the analyst's text is never kept"
        assert role.comp == "$150,000-$180,000"
        assert role.analysis_state == analysis.DONE and role.analysed_at is not None
        seen = analysis.view(role)
        assert [r["class"] for r in seen["required"]] == ["P", "U"]
        assert seen["for_owner"] == ["The posting asks for travel."]


async def test_a_requirement_may_cite_a_role_s_evidence_and_the_page_shows_its_text(js):
    from app.jobsearch.pipeline import page as roles

    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()
    body = findings(ids["Fabrikam"])
    body["required"][0]["from"] = ["e-board"]
    assert (await submit_findings(body))["taken"] is True
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        shown = await roles.requirements(session, OWNER, role)
    assert shown[0]["from"] == [{"id": "e-board", "text": "Board deck: Presented the risk register each quarter"}]


async def test_a_fact_the_owner_set_is_kept_and_a_gate_that_fails_on_the_facts_gates_the_role_out(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Initech"])
        employer = Employer(owner=OWNER, key="initech", name="Initech", employees=50, owner_fields=["employees"])
        session.add(employer)
        await session.flush()
        role.employer_id = employer.id
        await analysis.start(session, OWNER, role)
        await session.commit()
    reply = await submit_findings(findings(ids["Initech"], pay={"band": [60000, 70000]}))
    assert reply["stage"] == stages.GATED_OUT, "the researched band is under the $95,000 floor"
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Initech"])
        assert role.screened_out_reason.startswith("Pay must be at least $95,000 a year, on the estimated ask (")
        assert (await session.get(Employer, role.employer_id)).employees == 50, "the owner's fact stands"


async def test_a_researched_role_with_a_score_of_nothing_is_not_gated_for_it(js):
    """There is no lowest fit: the settle step runs the owner's gates again on the facts, and a low score is not one."""
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Hooli"]))
        await session.commit()
    reply = await submit_findings(findings(ids["Hooli"], fit_breakdown=answered("not", "not", "not")))
    assert (reply["taken"], reply["fit"], reply["fit_pct"], reply["stage"]) == (True, 0, 0, stages.READY_TO_LAUNCH)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Hooli"])
        # Researched, no gate failing and room on the list: it goes up to Ready to Launch, never closed on its score.
        assert (role.status, role.screened_out_reason, role.screened_out_by) == (stages.READY_TO_LAUNCH, None, None)
        assert role.analysis_state == analysis.DONE
        prof = await profile_store.current(session, OWNER)
        shown = role_gates.gate_results(role, prof, owner_today(), await session.get(Employer, role.employer_id))
    assert [g["id"] for g in shown if g["result"] == "fail"] == []
    assert not [g for g in shown if "fit" in g["id"]], "no gate on the page is about the fit"


async def test_a_re_judged_score_naming_an_ask_the_owner_doesnt_have_is_refused_with_the_problem(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await session.commit()
    bad = findings(ids["Fabrikam"], fit_breakdown={"asks": [{"id": "made-up", "answer": "met", "evidence": "?"}]})
    reply = await submit_findings(bad)
    assert reply["taken"] is False and "made-up" in reply["reason"] and reply["tries_left"] == 2
    old_way = findings(ids["Fabrikam"], fit_breakdown={"base": 2, "applied": []})
    reply = await submit_findings(old_way)
    assert reply["taken"] is False and "answer each of the owner's asks" in reply["reason"]
    good = findings(ids["Fabrikam"], fit_breakdown=answered("met", "partly", "not"))
    reply = await submit_findings(good)
    assert reply["taken"] is True and reply["fit"] == 2 and reply["fit_pct"] == 40, "the partly met ask earns nothing"
    assert reply["stage"] == stages.READY_TO_LAUNCH, "re-judged to 40%: a score never holds a role back"
    with pytest.raises(analysis.AnalysisError, match="isn't waiting"):
        await submit_findings(good)


async def test_an_analysis_never_sent_fails_in_the_sweep_and_the_mission_reports_what_came_of_it(js):
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await missions.ensure(session, OWNER)
        mission = next(m for m in (await session.scalars(select(missions.Mission))).all() if m.key == missions.ANALYSE)
        mission.settings = {**mission.settings, "target": 2}  # the mission's roles to research a night: two of three
        entry = await missions.start(session, OWNER, mission, by="you")
        assert entry.outcome == "running" and "Fabrikam" in entry.summary and "Hooli" not in entry.summary
        await session.commit()
    await submit_findings(findings(ids["Fabrikam"]))
    async with db.session_factory()() as session:
        # A session took Initech (its jobs.analysis call) and never sent it: the stuck time runs from then.
        assert (await analysis.claim_next(session, OWNER)).id == ids["Initech"]
        await session.commit()
        assert await analysis.settle_stuck(session, OWNER, now=datetime.now(UTC) + timedelta(hours=3)) == 1
        await session.commit()
        assert (await session.get(Opportunity, ids["Initech"])).analysis_state == analysis.FAILED
        entry = await session.get(missions.MissionRun, entry.id)
        await missions.refresh(session, entry)
        assert entry.outcome == "done"
        assert entry.summary == "Researched: 1 Ready to Launch, 1 failed. Stopped: target met."
