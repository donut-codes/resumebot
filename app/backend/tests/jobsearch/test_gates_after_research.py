"""When research is complete, the gates that could not be decided before it are checked again (the owner, 2026-10-13):
the analyst answers the sentence rules a role still has no answer to (never over a reader's), may fill a work location
and way of working the role lacks, the role's gates run again on all of it (a Found role is gated out or goes on, a
role already in Needs Drafted or Ready to Apply that now fails goes to Gated out unless the owner moved it by hand), and
a role in play with posting text kept but no answer to a rule or an ask goes to a reader whatever its route. Every
name, number and sentence is made up."""

from datetime import UTC, date, datetime

from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import scoring
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import sentence_gates
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import Profile
from app.jobsearch.research import analysis
from app.jobsearch.states import StopStep
from app.models.work import TrailEvent
from tests.builders import findings, save_documents, submit_findings
from tests.support import FIVE, OWNER, POSTING_WORDS, answered, load_profile

NOW = datetime.now(UTC)
TODAY = date.today()
TRAVEL = {"id": "travel", "kind": "rule", "section": "responsibilities", "text": "Travel of 20% or more."}
BADGE = {"id": "badge", "kind": "rule", "section": "responsibilities", "text": "The role needs an on-site badge."}
#: Only on site in one town is accepted, so a remote role fails the work location gate.
IN_OAKFIELD = {"in_person": {"on": True, "locations": ["Oakfield"]}}
EVIDENCE = "The posting says travel is up to 40% of the year."


def gate_answer(rule: dict = TRAVEL, answer: str = "fail", evidence: str = EVIDENCE) -> dict:
    return {"id": rule["id"], "answer": answer, "evidence": evidence}


def kept(rule: dict, answer: str = "pass", evidence: str = "A reader's line.") -> dict:
    """An answer to this rule in its words, as a role keeps it."""
    return {"id": rule["id"], "answer": answer, "evidence": evidence, "says": rule["text"]}


async def _world(*roles: tuple[str, str, dict], other: list[dict] | None = None, **gates) -> dict[str, Opportunity]:
    """The made-up profile with only these gates, and these roles `(company, stage, fields)` at 80% each with the
    analyst: the ids by company."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates={"other": other if other is not None else [TRAVEL],
                                                                 **gates})
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        profile = await profile_store.current(session, OWNER)
        made = {}
        for company, stage, fields in roles:
            breakdown = scoring.compute(answered(), profile)
            role = Opportunity(
                owner=OWNER, slug=company.lower(), company=company, title="Programs Lead", fit=breakdown["total"],
                origin="scout", match_key=match_key(company, "Programs Lead"), status=stage, found=TODAY,
                url=f"https://jobs.example.test/{company.lower()}", fit_breakdown=breakdown,
                **{"description": POSTING_WORDS, **fields})
            session.add(role)
            made[company] = role
        await session.flush()
        for role in made.values():
            # A role off the deck is only ever researched on the owner's ask (`pipeline.stop_waiting`).
            await analysis.start(session, OWNER, role, by_owner=role.status != stages.SECOND_IMPRESSION)
        await session.commit()
        return {company: role.id for company, role in made.items()}


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def _events(role_id) -> list[str]:
    async with db.session_factory()() as session:
        return [e.event for e in (await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == role_id).order_by(TrailEvent.at))).all()]


# --- the place research finds -------------------------------------------------------------------------------------


async def test_research_fills_an_empty_place_and_way_of_working_and_the_work_location_gate_now_stops_the_role(js):
    ids = await _world(("Fabrikam", stages.SECOND_IMPRESSION, {}), location=IN_OAKFIELD)
    assert (await _role(ids["Fabrikam"])).location is None
    reply = await submit_findings(findings(ids["Fabrikam"], location="  Remote,   US ", work_mode="Remote"))
    assert reply["stage"] == stages.GATED_OUT
    role = await _role(ids["Fabrikam"])
    assert (role.location, role.move_type) == ("Remote, US", "remote"), "tidied, and in the way the role keeps it"
    assert role.screened_out_by == "location" and "Remote, US" in role.screened_out_reason


async def test_a_place_the_role_already_has_is_never_overwritten_and_one_that_says_nothing_is_not_kept(js):
    ids = await _world(
        ("Fabrikam", stages.SECOND_IMPRESSION, {"location": "Oakfield", "move_type": "onsite"}),
        ("Initech", stages.SECOND_IMPRESSION, {"move_type": "unknown"}), ("Hooli", stages.SECOND_IMPRESSION, {}),
        location=IN_OAKFIELD)
    sent = {"location": "Remote, US", "work_mode": "remote"}
    assert (await submit_findings(findings(ids["Fabrikam"], **sent)))["stage"] == stages.READY_TO_LAUNCH
    role = await _role(ids["Fabrikam"])
    assert (role.location, role.move_type) == ("Oakfield", "onsite"), "it passes the gate on its own place"
    await submit_findings(findings(ids["Initech"], work_mode="hybrid"))
    assert (await _role(ids["Initech"])).move_type == "hybrid", "an unknown way of working says nothing: filled"
    await submit_findings(findings(ids["Hooli"], location="Not stated", work_mode="unknown"))
    role = await _role(ids["Hooli"])
    assert (role.location, role.move_type) == (None, None), "a place that is no place, and no way, are left out"


# --- the step a stop names -----------------------------------------------------------------------------------------


def test_a_stop_names_the_research_when_the_analyst_answered_and_the_reader_when_a_reader_did():
    profile = Profile.model_validate({"gates": {"other": [TRAVEL]}})
    rules = gating.Rules.of(profile, today=TODAY)

    def stop(answers):
        subject = gating.Subject(company="Fabrikam", location=None, work_mode=None, comp=None, comp_min=None,
                                 comp_max=None, posted=None, found=TODAY, answers=answers)
        return gating.check(subject, rules)

    research = stop([{**kept(TRAVEL, "fail", EVIDENCE), "by": "analyst"}])
    reader = stop([kept(TRAVEL, "fail", EVIDENCE)])
    assert research is not None and reader is not None
    assert (research.step, reader.step) == (StopStep.RESEARCH, StopStep.READER)
    assert research.role_words == reader.role_words and research.gate_id == reader.gate_id == "travel"
    # A reader's later answer in the same words takes the analyst's place, and then the stop is the reader's.
    later = sentence_gates.record([{**kept(TRAVEL, "pass"), "by": "analyst"}],
                                  [{"id": "travel", "answer": "fail", "evidence": EVIDENCE}], profile)
    assert [("by" in a, a["answer"]) for a in later] == [(False, "fail")]
    assert stop(later).step == StopStep.READER


# --- a role already past Found is gated again ---------------------------------------------------------------------


# --- a role no reader was routed to goes to one for its rules and asks ----------------------------------------------


async def _tracked(session, company, *, text="The whole posting.", status=stages.SECOND_IMPRESSION, route=None,
                   answers=None, prob=None, **more) -> Opportunity:
    """A role a tracker held (or an early search found), its posting a log entry of its own with no route."""
    employer = await employers.find_or_make(session, OWNER, company)
    role = Opportunity(
        owner=OWNER, slug=company.lower(), company=company, title="Programs Lead", status=status, origin="tracker",
        match_key=match_key(company, "Programs Lead"), found=TODAY, employer_id=employer.id, location="Remote, US",
        move_type="remote", url=f"https://jobs.example.test/{company.lower()}", gate_answers=answers or [],
        **{"description": POSTING_WORDS, **more})
    session.add(role)
    await session.flush()
    session.add(JobPosting(
        owner=OWNER, employer_id=employer.id, ats="import", ext_id=f"R-{company}", company=company,
        title="Programs Lead", match_key=role.match_key, url=role.url, location="Remote", work_mode="remote",
        text=text, route=route, first_seen=NOW, last_seen=NOW, filtered_at=NOW, links=[], fit_prob=prob,
        opportunity_id=role.id))
    return role
