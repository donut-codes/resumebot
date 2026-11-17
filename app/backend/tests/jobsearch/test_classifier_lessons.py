"""The owner's decisions teach the right lesson (docs/PRD.md 31.7): the classifier's "passed on" examples are only what
the owner or the title check said about the kind of job, never a role a place, pay, employer or age gate stopped; a
role the employer turned down, and an imported role the owner applied to, count as moved forward even with no stage
history; and the page can say what it learned from. Synthetic roles only: every title and word here is made up."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import actions
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import classifier
from app.jobsearch.imports import tracker
from app.jobsearch.models import JobPosting, Opportunity, StageMove
from app.jobsearch.profile import gates as pg
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import views as pv
from app.jobsearch.states import StopGate
from app.sites import avoid
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile, tracker_row, tracker_workbook

NOW = datetime(2026, 10, 9, 5, 30, tzinfo=UTC)
BODY = "You will lead the work with partners across the business and report to the leadership team."


@pytest.fixture(autouse=True)
def fresh_cache():
    classifier.clear_cache()
    yield
    classifier.clear_cache()


def _role(n, title, **more):
    """A made-up role; one given a stop's sentence is stopped by the gate that gives it (as the app records it)."""
    more.setdefault("screened_out_by", GATE_OF.get(more.get("screened_out_reason")))
    return Opportunity(owner=OWNER, slug=f"co-{n}", company=f"Co {n}", title=title, match_key=f"co {n}|{title.lower()}",
                       origin="scout", **more)


async def _learned(session):
    """What the classifier learns from the owner's roles: title -> (positive, weight)."""
    await load_profile(session, OWNER)
    examples = await classifier.examples(session, OWNER)
    return {text.split(".")[0]: (positive, weight) for text, positive, weight in examples}


# --- which stops are about the kind of job ---------------------------------------------------------------------------


def real_reasons() -> dict[str, str]:
    """The sentences the app really gives a stop, from the made-up profile's gates and the process's own words."""
    says = pv.engine_gates(p.Profile.model_validate(PROFILE)).says
    age = pg.gate_says(pg.Gate(id="age", kind="posting_age", days=30))
    return {
        "place": actions._why(says["location"], "posted: Gotham"),
        "pay": actions._why(says["compensation"], "the ask is $70,000"),
        "age": actions._why(age, "this one is 60 days old"),
        "employer": "an employer you never work for", "avoid": avoid.REASON,
        # What the classifier said while it still stopped postings (until 2026-10-13): a closed one still carries it.
        "unlikely": "unlikely for you (0.08)",
        "title-pass": "not your kind of job: it is a warehouse role",
    }


#: The gate each of those sentences comes from, as a stopped role records it (`Opportunity.screened_out_by`).
GATES = {
    "place": StopGate.LOCATION, "pay": StopGate.COMPENSATION, "age": StopGate.AGE, "employer": StopGate.EMPLOYER,
    "avoid": StopGate.OTHER, "title-pass": StopGate.KIND,
}
GATE_OF = {reason: GATES[name] for name, reason in real_reasons().items() if name in GATES}


def test_only_what_is_said_about_the_kind_of_job_is_a_lesson_about_it():
    teaches = {name: classifier.kind_of_job_stop(gate) for name, gate in GATES.items()}
    assert teaches == {"place": False, "pay": False, "age": False, "employer": False, "avoid": False,
                       "title-pass": True}
    assert classifier.kind_of_job_stop(None) is False and classifier.kind_of_job_stop("") is False


async def test_each_screen_action_names_the_gate_that_stopped_the_role_so_no_sentence_has_to_be_read():
    """What a stopped role teaches rests on the gate that stopped it, never on how its sentence is worded."""
    role = {"key": "k", "fit": 2, "location": "Gotham", "comp_judged": 70_000, "comp_floor": 90_000, "age_days": 60,
            "stale_days": 30}
    data = {"role": role, "gates": {"comp_floor": 90_000, "stale_days": 30}}
    stops = {
        StopGate.EMPLOYER: actions.drop_excluded, StopGate.LOCATION: actions.drop_location,
        StopGate.COMPENSATION: actions.drop_comp, StopGate.AGE: actions.drop_stale,
    }
    for gate, action in stops.items():
        verdict = await action(None, None, data)
        assert verdict["keep"] is False and verdict["gate"] == gate and verdict["reason"], gate
    kept = await actions.keep(None, None, data)
    assert kept["keep"] is True and kept["gate"] is None


# --- the owner's roles: what they teach ------------------------------------------------------------------------


async def test_a_role_a_place_pay_employer_or_age_gate_stopped_is_no_example_at_all(js):
    reasons = real_reasons()
    async with db.session_factory()() as session:
        session.add_all([
            _role(i, f"Stopped For {name}", status="gated_out", fit=4, screened_out_reason=reason)
            for i, (name, reason) in enumerate(
                {k: reasons[k] for k in ("place", "pay", "age", "employer", "avoid")}.items())])
        session.add(_role(9, "Stopped With No Reason", status="gated_out", fit=1))
        await session.flush()
        learned = await _learned(session)
    assert learned == {}, "a place-gated role would teach 'wrong place', not 'wrong kind of job'"


async def test_a_role_stopped_as_not_the_kind_of_job_is_a_passed_on_example_and_a_score_teaches_nothing(js):
    reasons = real_reasons()
    async with db.session_factory()() as session:
        session.add_all([
            # A score, low or high, stops no role and teaches nothing: it ranks (docs/PRD.md 39.10), and it is worked
            # out after research, so it says nothing of the owner's kind of job (there is no research bar to clear).
            _role(1, "Low Fit", status="second_impression", fit=0),
            _role(3, "High Fit", status="second_impression", fit=5),
            _role(2, "Not The Kind", status="gated_out", fit=4, screened_out_reason=reasons["title-pass"])])
        await session.flush()
        learned = await _learned(session)
    assert learned == {"Not The Kind": (False, 1.0)}


async def test_a_role_the_owner_passed_on_is_a_passed_on_example_unless_they_had_applied(js):
    async with db.session_factory()() as session:
        session.add_all([
            _role(1, "Passed Early", status="withdrew", ended_reason="i_passed", fit=4),
            _role(2, "Applied Then Passed", status="applied", fit=1)])
        await session.flush()
        later = (await session.scalars(select(Opportunity).where(Opportunity.title == "Applied Then Passed"))).one()
        later.status, later.ended_reason = "withdrew", "i_passed"
        await session.flush()
        learned = await _learned(session)
    assert learned == {"Passed Early": (False, 1.0), "Applied Then Passed": (True, 2.0)}, (
        "passing after applying is a role they pursued")


async def test_the_kind_of_job_reasons_are_passed_on_examples_and_duplicate_or_something_else_teach_nothing(js):
    async with db.session_factory()() as session:
        reasons = ("not_my_field", "wrong_level", "pay_too_low", "location_or_mode", "the_employer", "duplicate",
                   "something_else")
        session.add_all([_role(i, f"Reason {r}", status="withdrew", ended_reason=r, fit=4)
                         for i, r in enumerate(reasons, start=1)])
        await session.flush()
        learned = await _learned(session)
    assert learned == {f"Reason {r}": (False, 1.0) for r in reasons[:5]}


async def test_a_role_the_employer_turned_down_counts_as_moved_forward_even_with_no_stage_history(js):
    async with db.session_factory()() as session:
        session.add_all([_role(1, "Turned Down", status="rejected", fit=1), _role(2, "Went Quiet", status="gone_quiet",
                                                                                   fit=1),
                         _role(3, "Posting Went", status="withdrew", ended_reason="posting_gone", fit=1)])
        await session.flush()
        assert await session.scalar(select(StageMove.id).where(StageMove.to_status == "applied")) is None
        learned = await _learned(session)
    assert learned["Turned Down"] == (True, 2.0)
    assert "Went Quiet" not in learned and "Posting Went" not in learned, (
        "a posting that came down, or an employer that went quiet, says nothing of the job; a score does not either")


async def test_an_imported_role_the_owner_applied_to_counts_as_moved_forward_with_no_stage_history(js):
    rows = [tracker_row("Acme", "Imported Applied", status="applied", fit=1),
            tracker_row("Acme", "Imported Interviewing", status="interviewing", fit=1),
            tracker_row("Acme", "Imported Rejected", status="rejected", fit=1),
            tracker_row("Acme", "Imported Screened Out", status="screened_out", fit=4)]
    async with db.session_factory()() as session:
        await tracker.import_tracker(session, OWNER, tracker_workbook(rows))
        assert await session.scalar(select(StageMove.id)) is None, "an import writes no stage history"
        learned = await _learned(session)
    assert learned == {"Imported Applied": (True, 2.0), "Imported Interviewing": (True, 2.0),
                       "Imported Rejected": (True, 2.0)}, "a screened-out row, which names no gate, is left out"


# --- the postings it is balanced against -----------------------------------------------------------------------


def _posting(employer, ext_id, title, **more):
    """A made-up posting; one given a stop's sentence was stopped by whoever gives it (as the app records it)."""
    why = more.get("filtered_by")
    if why:
        more.setdefault("judged_by", "titles" if why.startswith("not your kind") else
                        "classifier" if why.startswith("unlikely for you") else "gate")
    return JobPosting(owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=ext_id, company=employer.name,
                      title=title, first_seen=NOW, last_seen=NOW, links=[], location="Remote", work_mode="remote",
                      **more)


async def test_the_postings_it_is_balanced_against_leave_out_the_ones_a_place_or_pay_gate_stopped(js):
    reasons = real_reasons()
    async with db.session_factory()() as session:
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([
            _posting(acme, "waiting", "Waiting Posting", text=BODY),
            _posting(acme, "kind", "Kind Of Job Posting", filtered_by=reasons["title-pass"]),
            _posting(acme, "place", "Place Posting", filtered_by=reasons["place"]),
            _posting(acme, "pay", "Pay Posting", filtered_by=reasons["pay"]),
            _posting(acme, "age", "Age Posting", filtered_by=reasons["age"]),
            _posting(acme, "employer", "Employer Posting", filtered_by=reasons["employer"]),
            # A stop the classifier made when it still made them: it never learns from its own.
            _posting(acme, "own", "Classifier Posting", filtered_by=reasons["unlikely"])])
        await session.flush()
        background = await classifier.background(session, OWNER, 50.0, 0.0)
    assert sorted(t.split(".")[0] for t, _positive, _w in background) == ["Kind Of Job Posting", "Waiting Posting"]
    assert not any(positive for _t, positive, _w in background)


async def test_the_model_is_trained_again_when_the_owner_says_why_a_role_ended(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "classifier_min_examples": 5})
        for n in range(6):
            session.add(_role(f"p{n}", f"Governance Lead {n}", status="applied", fit=4, description=BODY))
        session.add(_role("n", "Warehouse Packer", status="withdrew", ended_reason="i_passed", fit=1, description=BODY))
        quiet = _role("q", "Payroll Clerk", status="withdrew", fit=1, description=BODY)
        session.add(quiet)
        await session.flush()
        first = await classifier.train(session, OWNER)
        assert first is not None and await classifier.train(session, OWNER) is first
        quiet.ended_reason = "i_passed"
        await session.flush()
        assert await classifier.train(session, OWNER) is not first, "told it passed on it: it learns again"
